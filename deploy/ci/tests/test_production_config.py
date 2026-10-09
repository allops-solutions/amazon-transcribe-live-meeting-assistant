"""Offline checks for the approved initial release and secret-safe diagnostics."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('production_config', ROOT / 'deploy/ci/production_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ProductionConfigTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / 'deploy/params/prod.json').read_text())

    def test_only_google_secret_requires_user_input(self):
        self.assertEqual(config.issues(self.base, {}), [
            {'parameter': 'GoogleOAuthClientSecret', 'reason': 'unresolved placeholder'}])
        self.assertEqual(config.issues(self.base, {'GoogleOAuthClientSecret': 'test-secret-not-real'}), [])

    def test_unapproved_optional_credentials_are_not_silently_activated(self):
        for key in config.DISABLED:
            with self.subTest(key=key):
                found = config.issues(self.base, {'GoogleOAuthClientSecret': 'test-secret-not-real', key: 'secret-do-not-print'})
                self.assertEqual(found[0]['parameter'], key)
                self.assertNotIn('secret-do-not-print', json.dumps(found))

    def test_sso_and_release_overrides_cannot_remove_required_settings(self):
        for key, value in [('GoogleOAuthClientSecret', ''), ('GoogleOAuthClientId', ''),
                           ('EnableGoogleSso', 'false'), ('VPLaunchType', 'EC2'),
                           ('GoogleAdminEmail', 'someone-else@allops.co')]:
            with self.subTest(key=key):
                overrides = {'GoogleOAuthClientSecret': 'test-secret-not-real', key: value}
                self.assertIn(key, [item['parameter'] for item in config.issues(self.base, overrides)])

    def test_bad_input_diagnostics_do_not_reveal_values(self):
        for overrides in [[], {'Unknown': 'never-print-this'}, {'GoogleOAuthClientSecret': 123}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError) as error:
                config.issues(self.base, overrides)
            self.assertNotIn('never-print-this', str(error.exception))


if __name__ == '__main__':
    unittest.main()
