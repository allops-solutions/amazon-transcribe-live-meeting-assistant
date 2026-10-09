# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Offline checks for capability-specific ceilings; no policies are attached."""
import json
import unittest
from unittest.mock import MagicMock

from test_runtime_boundary import boundary, service_fixture, ACCOUNT, REGION, BUCKET, DOCS
from model_resources import PROFILES
from simulate_runtime_capabilities import check, FAMILIES


class RuntimeCapabilityTests(unittest.TestCase):
    def build(self, capabilities, **kwargs):
        return boundary.build_policy(ACCOUNT, REGION, BUCKET, DOCS,
                                     capabilities=capabilities, **kwargs)

    def statement(self, policy, sid):
        return next(value for value in policy['Statement'] if value.get('Sid') == sid)

    def test_default_document_is_unchanged_by_explicit_none(self):
        self.assertEqual(boundary.build_policy(ACCOUNT, REGION, BUCKET),
                         boundary.build_policy(ACCOUNT, REGION, BUCKET, capabilities=None))

    def test_no_arbitrary_or_empty_capability_selection(self):
        for values in [[], {}, 'Lambda', ['*'], ['Lambda', 'Lambda'], [None], ['ProvisionAnything']]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.build(values)

    def test_selection_removes_other_actions_resources_and_passrole(self):
        policy = self.build(['DynamoDB', 'Logs'])
        actions = self.statement(policy, 'DenyUnreviewedActions')['NotAction']
        self.assertIn('dynamodb:*', actions)
        self.assertNotIn('iam:PassRole', actions)
        self.assertNotIn('lambda:*', actions)
        self.assertFalse(any(value.get('Sid') == 'AllowPassApplicationRoles' for value in policy['Statement']))
        scopes = self.statement(policy, 'DenyOutsideNamedResources')['NotResource']
        self.assertFalse(any(':lambda:' in value or ':iam:' in value for value in scopes))

    def test_streaming_is_explicit_and_region_pinned(self):
        policy = self.build(['StreamingTranscription'])
        actions = self.statement(policy, 'DenyUnreviewedActions')['NotAction']
        self.assertIn('transcribe:StartStreamTranscription', actions)
        self.assertNotIn('transcribe:GetTranscriptionJob', actions)
        self.assertNotIn('transcribe:ListTranscriptionJobs', actions)
        self.assertNotIn('transcribe:GetVocabulary', actions)
        deny = self.statement(policy, 'DenyComputationOutsideRegion')
        self.assertEqual(deny['Condition'], {
            'StringNotEqualsIfExists': {'aws:RequestedRegion': REGION}})
        self.assertEqual(deny['Effect'], 'Deny')
        named = self.statement(policy, 'DenyOutsideNamedResources')
        self.assertEqual(named['Resource'], '*')
        self.assertNotIn('NotResource', named)

    def test_text_processing_does_not_authorize_custom_models_or_stored_jobs(self):
        policy = self.build(['TextProcessing'])
        actions = self.statement(policy, 'DenyUnreviewedActions')['NotAction']
        self.assertIn('comprehend:DetectSentiment', actions)
        self.assertIn('translate:TranslateText', actions)
        self.assertNotIn('comprehend:CreateEndpoint', actions)
        self.assertNotIn('comprehend:DetectEntities', actions)
        self.assertNotIn('translate:TranslateDocument', actions)

    def test_unselected_generated_inputs_are_still_validated(self):
        with self.assertRaises(ValueError):
            self.build(['Logs'], generated_resources={'kms': ['*']})
        with self.assertRaises(ValueError):
            self.build(['Logs'], model_resources={'profiles': [], 'models': ['*']})

    def test_selected_definition_without_cluster_input_still_cannot_launch(self):
        policy = self.build(['EcsDefinitions'], generated_resources={
            'ecs_task_definitions': service_fixture()['ecs_task_definitions']})
        self.assertNotIn('ecs:RunTask', self.statement(policy, 'DenyUnreviewedActions')['NotAction'])

    def test_only_launch_capability_gets_cluster_deny_not_unrelated_metadata(self):
        policy = self.build(['EcsDefinitions'], generated_resources=service_fixture())
        deny = self.statement(policy, 'DenyEcsLaunchOutsideReviewedClusters')
        self.assertEqual(deny['Action'], ['ecs:RunTask'])
        self.assertEqual(deny['Condition']['ArnNotEqualsIfExists']['ecs:cluster'],
                         service_fixture()['ecs_clusters'])
        policy = self.build(['Logs'], generated_resources=service_fixture())
        self.assertFalse(any('EcsLaunch' in value.get('Sid', '') for value in policy['Statement']))

    def test_split_fixture_fits_with_computation_without_dropping_selected_scopes(self):
        profiles = [f'arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{name}' for name in PROFILES]
        models = [f'arn:aws:bedrock:{REGION}::foundation-model/{name.removeprefix("global.")}'
                  for name in PROFILES]
        models.append(f'arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0')
        families = [
            ['DynamoDB', 'Lambda', 'Kinesis', 'Logs', 'KmsData', 'AppSyncData',
             'StreamingTranscription', 'TextProcessing'],
            ['DynamoDB', 'Logs', 'KmsData', 'KnowledgeBases', 'VectorIndexes',
             'ModelProfiles', 'ModelInvocation'],
            ['PassApplicationRoles', 'Lambda', 'Logs', 'EcsClusters', 'EcsTasks',
             'EcsDefinitions', 'Schedules', 'StateMachines', 'Executions'],
        ]
        for family in families:
            with self.subTest(family=family):
                policy = self.build(family, generated_resources=service_fixture(),
                                    model_resources={'profiles': profiles, 'models': models})
                self.assertLessEqual(len(json.dumps(policy, separators=(',', ':'))), 6144)
                self.assertTrue(any('LMA-Isolation-*' in json.dumps(value)
                                    and value['Effect'] == 'Deny' for value in policy['Statement']))

    def test_read_only_probe_uses_boundary_input_and_rejects_incomplete_validation(self):
        iam, analyzer = MagicMock(), MagicMock()
        analyzer.validate_policy.return_value = {'findings': []}
        for family, (_, allowed, forbidden) in FAMILIES.items():
            iam.reset_mock()
            iam.simulate_custom_policy.side_effect = [
                {'EvaluationResults': [{'EvalDecision':
                    'allowed' if action in allowed and requested == REGION else 'explicitDeny'}]}
                for action in allowed + forbidden for requested in [REGION, 'eu-west-1', '']]
            result = check(iam, analyzer, ACCOUNT, REGION, family)
            self.assertFalse(result['deploymentReady'])
            self.assertFalse(result['attached'])
            self.assertEqual({call[0] for call in iam.method_calls}, {'simulate_custom_policy'})
            for call in iam.simulate_custom_policy.call_args_list:
                self.assertIn('PermissionsBoundaryPolicyInputList', call.kwargs)
                self.assertEqual(len(call.kwargs['PolicyInputList']), 1)
        for response in [{}, {'findings': [], 'nextToken': 'more'}, {'findings': [{}]}]:
            analyzer.validate_policy.return_value = response
            with self.assertRaises(ValueError):
                check(iam, analyzer, ACCOUNT, REGION, 'speech')

    def test_probe_fails_on_simulator_truncation_or_wrong_decision(self):
        iam, analyzer = MagicMock(), MagicMock()
        analyzer.validate_policy.return_value = {'findings': []}
        for response in [{}, {'EvaluationResults': [{'EvalDecision': 'explicitDeny'}]},
                         {'IsTruncated': True, 'EvaluationResults': [{'EvalDecision': 'allowed'}]}]:
            iam.simulate_custom_policy.return_value = response
            with self.assertRaises(RuntimeError):
                check(iam, analyzer, ACCOUNT, REGION, 'speech')


if __name__ == '__main__':
    unittest.main()
