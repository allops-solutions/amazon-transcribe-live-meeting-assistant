"""Offline release request-guard regression tests; no AWS access."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('release_guards', ROOT / 'deploy/ci/simulate_release_guards.py')
guards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guards)


class ReleaseGuardTests(unittest.TestCase):
    def test_exact_scopes_and_no_unresolved_template_values(self):
        policy, role, bucket = guards.load_policy('009853297978')
        self.assertEqual(len(policy['Statement']), 3)
        self.assertNotIn('${', str(policy))
        self.assertIn('/lma/isolation/LMA-', role)
        self.assertEqual(bucket, 'allops-lma-ci-009853297978-production-us-east-1')
        self.assertEqual(policy['Statement'][0]['Resource'], 'arn:aws:cloudformation:us-east-1:009853297978:stack/LMA/*')

    def test_simulation_only_calls_read_only_iam_api(self):
        client = MagicMock()
        client.simulate_custom_policy.side_effect = [
            {'EvaluationResults': [{'EvalDecision': d}]} for d in ['allowed'] + ['explicitDeny'] * 5]
        guards.simulate(client, '009853297978')
        self.assertEqual(client.simulate_custom_policy.call_count, 6)
        self.assertEqual({call[0] for call in client.method_calls}, {'simulate_custom_policy'})

    def test_invalid_account_and_unexpected_result_fail_closed(self):
        with self.assertRaises(ValueError):
            guards.load_policy('*')
        client = MagicMock()
        client.simulate_custom_policy.return_value = {'EvaluationResults': [{'EvalDecision': 'implicitDeny'}]}
        with self.assertRaises(RuntimeError):
            guards.simulate(client, '009853297978')
