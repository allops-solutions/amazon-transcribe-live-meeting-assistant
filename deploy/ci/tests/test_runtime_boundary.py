"""Offline contract tests for the inactive runtime ceiling; no AWS access."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('runtime_boundary', ROOT / 'deploy/ci/runtime_boundary.py')
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
ACCOUNT = '009853297978'
REGION = 'us-east-1'
BUCKET = f'allops-lma-ci-{ACCOUNT}-production-{REGION}'
DOCS = f'allops-lma-documents-{ACCOUNT}-{REGION}'


def generated_fixture():
    """Synthetic IDs for offline/simulator tests, not verified AWS ownership."""
    ecs = f'arn:aws:ecs:{REGION}:{ACCOUNT}:'
    return {
        'kms': [f'arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789abc'],
        'appsync': [f'arn:aws:appsync:{REGION}:{ACCOUNT}:apis/abcdefghijklmnop'],
        'cognito': [f'arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/{REGION}_Example12'],
        'cloudfront': [f'arn:aws:cloudfront::{ACCOUNT}:distribution/E123456789ABCD'],
        'ecs_clusters': [ecs + 'cluster/LMA-VP-Cluster'],
        'ecs_task_definitions': [ecs + 'task-definition/LMA-VP-Task:1'],
    }


class RuntimeBoundaryTests(unittest.TestCase):
    def test_combined_services_fit_and_enforce_launch_cluster(self):
        generated = generated_fixture()
        policy = boundary.build_policy(ACCOUNT, REGION, BUCKET, DOCS, generated_resources=generated)
        self.assertLessEqual(len(json.dumps(policy, separators=(',', ':'))), 6144)
        deny = next(s for s in policy['Statement'] if s.get('Condition', {}).get('ArnNotEqualsIfExists'))
        self.assertEqual(set(deny['Action']), {'ecs:RunTask', 'ecs:ListTasks'})
        self.assertEqual(deny['Condition']['ArnNotEqualsIfExists']['ecs:cluster'], generated['ecs_clusters'])
        ceiling = next(s for s in policy['Statement'] if s['Effect'] == 'Allow' and 'NotAction' in s)
        self.assertEqual(ceiling['NotAction'], 'iam:*')

    def test_definition_without_cluster_cannot_launch(self):
        generated = {'ecs_task_definitions': generated_fixture()['ecs_task_definitions']}
        policy = boundary.build_policy(ACCOUNT, REGION, BUCKET, generated_resources=generated)
        actions = next(s for s in policy['Statement'] if s.get('Sid') == 'DenyUnreviewedActions')['NotAction']
        self.assertNotIn('ecs:RunTask', actions)
        self.assertNotIn('ecs:DescribeTaskDefinition', actions)

    def test_all_generated_services_reject_foreign_wildcard_and_invalid_ids(self):
        for group, arns in generated_fixture().items():
            for value in [arns[0].replace(ACCOUNT, '111111111111'), arns[0] + '*']:
                with self.subTest(group=group, value=value), self.assertRaises(ValueError):
                    boundary.build_policy(ACCOUNT, REGION, BUCKET, generated_resources={group: [value]})

    def test_oversized_inventory_fails_without_dropping_scopes(self):
        with self.assertRaisesRegex(ValueError, 'size limit'):
            boundary.build_policy(ACCOUNT, REGION, BUCKET, generated_resources={
                'cloudfront': [f'arn:aws:cloudfront::{ACCOUNT}:distribution/E{n:013d}' for n in range(100)]})

    def test_generated_scopes_are_exact_and_fit_with_documents(self):
        generated = {'kms': [f'arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789abc'],
                     'appsync': [f'arn:aws:appsync:{REGION}:{ACCOUNT}:apis/abcdefghijklmnop']}
        policy = boundary.build_policy(ACCOUNT, REGION, BUCKET, DOCS, generated_resources=generated)
        self.assertLessEqual(len(json.dumps(policy, separators=(',', ':'))), 6144)
        scopes = next(s for s in policy['Statement'] if 'NotResource' in s and
                      'iam:PassRole' in s.get('Action', []))['NotResource']
        self.assertIn(generated['kms'][0], scopes)
        self.assertIn(generated['appsync'][0] + '/*', scopes)
        for invalid in [[], {'unknown': []}, {'kms': ['*']},
                        {'kms': [generated['kms'][0].replace(ACCOUNT, '111111111111')]}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                boundary.build_policy(ACCOUNT, REGION, BUCKET, generated_resources=invalid)

    def test_managed_policy_size_with_and_without_documents(self):
        for docs in ['', DOCS]:
            with self.subTest(docs=docs):
                policy = boundary.build_policy(ACCOUNT, REGION, BUCKET, docs)
                self.assertLessEqual(len(json.dumps(policy, separators=(',', ':'))), 6144)

    def test_unreviewed_services_fail_closed(self):
        policy = boundary.build_policy(ACCOUNT, REGION, BUCKET)
        deny = next(s for s in policy['Statement'] if s['Sid'] == 'DenyUnreviewedActions')
        self.assertEqual(deny['Effect'], 'Deny')
        self.assertEqual(deny['Resource'], '*')
        self.assertTrue(all(not a.startswith(('sts:', 'ec2:', 'kms:', 'appsync:'))
                            for a in deny['NotAction']))
        self.assertEqual([a for a in deny['NotAction'] if a.startswith('iam:')], ['iam:PassRole'])

    def test_artifact_prefix_is_read_only(self):
        policy = boundary.build_policy(ACCOUNT, REGION, BUCKET)
        deny = next(s for s in policy['Statement'] if s['Sid'] == 'ArtifactsAreReadOnly')
        self.assertEqual(set(deny['NotAction']), {'s3:GetObject', 's3:GetObjectVersion', 's3:ListBucket', 's3:GetBucketLocation'})
        scope = next(s for s in policy['Statement'] if s['Sid'] == 'DenyS3OutsideLma')
        self.assertIn(f'arn:aws:s3:::{BUCKET}/releases/*', scope['NotResource'])

    def test_inputs_cannot_broaden_resource_scope(self):
        for account, region, bucket, docs, partition in [
            ('*', REGION, BUCKET, '', 'aws'), (ACCOUNT, '*', BUCKET, '', 'aws'),
            (ACCOUNT, REGION, '*', '', 'aws'), (ACCOUNT, REGION, BUCKET, 'company-finance', 'aws'),
            (ACCOUNT, REGION, BUCKET, DOCS, '*'),
        ]:
            with self.subTest(account=account, region=region, bucket=bucket, docs=docs), self.assertRaises(ValueError):
                boundary.build_policy(account, region, bucket, docs, partition)

    def test_read_only_simulation_checks_all_cases(self):
        client = MagicMock()
        cases = boundary.simulation_cases(ACCOUNT, REGION, BUCKET)
        client.simulate_custom_policy.side_effect = [
            {'EvaluationResults': [{'EvalDecision': row[-1]}]} for row in cases]
        boundary.simulate(client, boundary.build_policy(ACCOUNT, REGION, BUCKET), ACCOUNT, REGION, BUCKET)
        self.assertEqual(client.simulate_custom_policy.call_count, 25)
        self.assertEqual({call[0] for call in client.method_calls}, {'simulate_custom_policy'})

    def test_unexpected_simulator_result_is_failure(self):
        client = MagicMock()
        client.simulate_custom_policy.return_value = {'EvaluationResults': [{'EvalDecision': 'explicitDeny'}]}
        with self.assertRaisesRegex(RuntimeError, 'lma-table-write'):
            boundary.simulate(client, boundary.build_policy(ACCOUNT, REGION, BUCKET), ACCOUNT, REGION, BUCKET)

    def test_other_partitions_do_not_emit_commercial_arns(self):
        for partition, region in [('aws-us-gov', 'us-gov-west-1'), ('aws-cn', 'cn-north-1')]:
            with self.subTest(partition=partition):
                bucket = f'allops-lma-ci-{ACCOUNT}-production-{region}'
                docs = f'allops-lma-documents-{ACCOUNT}-{region}'
                policy = boundary.build_policy(ACCOUNT, region, bucket, docs, partition)
                self.assertNotIn('arn:aws:', json.dumps(policy))


if __name__ == '__main__':
    unittest.main()
