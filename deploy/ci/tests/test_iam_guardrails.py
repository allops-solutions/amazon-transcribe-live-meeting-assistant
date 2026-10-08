"""Offline guardrail contract tests; AWS simulation is a separate read-only command."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock

import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('simulate_guardrails',
                                            ROOT / 'deploy/ci/simulate_iam_guardrails.py')
guardrails = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guardrails)


class GuardrailTests(unittest.TestCase):
    def test_guardrail_template_grants_no_permissions_and_is_disabled(self):
        template = yaml.load((ROOT / 'deploy/ci/iam-guardrails.yaml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(template['Parameters']['EnableGuardrails']['Default'], 'false')
        self.assertEqual(set(template['Resources']), {'ProvisioningIamGuardrails'})
        resource = template['Resources']['ProvisioningIamGuardrails']
        self.assertEqual(resource['Condition'], 'Enabled')
        self.assertNotIn('Roles', resource['Properties'])
        policy, boundary = guardrails.load_policy('009853297978')
        self.assertTrue(all(s['Effect'] == 'Deny' for s in policy['Statement']))
        required = policy['Statement'][0]
        self.assertEqual(required['Condition']['ArnNotEquals']['iam:PermissionsBoundary'], boundary)

    def test_simulation_does_not_attach_or_create_anything(self):
        client = MagicMock()
        policy, boundary = guardrails.load_policy('009853297978')
        cases = guardrails.cases('009853297978', boundary)
        client.simulate_custom_policy.side_effect = [
            {'EvaluationResults': [{'EvalDecision': row[-1]}]} for row in cases]
        guardrails.simulate(client, '009853297978')
        self.assertEqual(client.simulate_custom_policy.call_count, 19)
        self.assertEqual({call[0] for call in client.method_calls}, {'simulate_custom_policy'})

    def test_simulation_fails_on_incorrect_decision(self):
        client = MagicMock()
        client.simulate_custom_policy.return_value = {'EvaluationResults': [{'EvalDecision': 'explicitDeny'}]}
        with self.assertRaisesRegex(RuntimeError, 'create-bounded-role'):
            guardrails.simulate(client, '009853297978')


if __name__ == '__main__':
    unittest.main()
