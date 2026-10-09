"""Approved profile identity and destination tests; no inference calls."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('model_resources', ROOT / 'deploy/ci/model_resources.py')
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)
ACCOUNT, REGION = '009853297978', 'us-east-1'


def fixture():
    return [{'status': 'ACTIVE',
             'inferenceProfileArn': f'arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{identifier}',
             'models': [{'modelArn': f'arn:aws:bedrock:::foundation-model/{identifier.removeprefix("global.")}'},
                        {'modelArn': f'arn:aws:bedrock:{REGION}::foundation-model/{identifier.removeprefix("global.")}'}]}
            for identifier in models.PROFILES]


class ModelResourcesTests(unittest.TestCase):
    def test_exact_active_destinations_and_no_inference(self):
        client = MagicMock()
        client.get_inference_profile.side_effect = fixture()
        result = models.collect(client, ACCOUNT, REGION)
        models.validate(result, ACCOUNT, REGION)
        self.assertEqual(len(result['profiles']), 3)
        self.assertEqual(len(result['models']), 7)
        self.assertEqual({call[0] for call in client.method_calls}, {'get_inference_profile'})

    def test_foreign_inactive_empty_and_unapproved_destinations_fail_closed(self):
        records = fixture()
        for key, value in [('status', 'INACTIVE'), ('models', []),
                           ('inferenceProfileArn', records[0]['inferenceProfileArn'].replace(ACCOUNT, '111111111111')),
                           ('models', [{'modelArn': 'arn:aws:bedrock:us-east-1::foundation-model/*'}])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                results = fixture()
                results[0][key] = value
                client = MagicMock()
                client.get_inference_profile.side_effect = results
                models.collect(client, ACCOUNT, REGION)

    def test_builder_rejects_arbitrary_or_wildcard_models(self):
        for value in ['arn:aws:bedrock:*::foundation-model/*',
                      'arn:aws:bedrock:us-east-1::foundation-model/unapproved',
                      f'arn:aws:bedrock:us-east-1:{ACCOUNT}:custom-model/example']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                models.validate({'profiles': [], 'models': [value]}, ACCOUNT, REGION)


if __name__ == '__main__':
    unittest.main()
