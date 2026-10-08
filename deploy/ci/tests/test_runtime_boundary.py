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


class RuntimeBoundaryTests(unittest.TestCase):
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
        self.assertTrue(all(not a.startswith(('iam:', 'sts:', 'ec2:', 'kms:', 'appsync:'))
                            for a in deny['NotAction']))

    def test_artifact_prefix_is_read_only(self):
        policy = boundary.build_policy(ACCOUNT, REGION, BUCKET)
        read = next(s for s in policy['Statement'] if s['Sid'] == 'AllowReleaseRead')
        self.assertEqual(set(read['Action']), {'s3:GetObject', 's3:GetObjectVersion', 's3:ListBucket', 's3:GetBucketLocation'})
        self.assertIn(f'arn:aws:s3:::{BUCKET}/releases/*', read['Resource'])
        deny = next(s for s in policy['Statement'] if s['Sid'] == 'ArtifactsAreReadOnly')
        self.assertEqual(deny['NotAction'], read['Action'])

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
        self.assertEqual(client.simulate_custom_policy.call_count, 20)
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
