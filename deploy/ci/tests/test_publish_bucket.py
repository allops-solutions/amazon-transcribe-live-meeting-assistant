"""Execute both publishers' bucket preflight with mocked AWS; no network or builds."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ('lma-bedrockkb-stack/publish.sh', 'lma-meetingassist-setup-stack/publish.sh')
FAKE_AWS = '''#!/usr/bin/env python3
import json
import os
import sys
with open(os.environ['MOCK_CALLS'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['s3api', 'head-bucket']:
    error = os.environ['MOCK_HEAD_ERROR']
    if error:
        print(error, file=sys.stderr)
        sys.exit(255)
'''


class PublishBucketTests(unittest.TestCase):
    def run_preflight(self, script, error=''):
        """Run the actual script through bucket preflight only, replacing aws."""
        source = (ROOT / script).read_text()
        preflight, marker, _ = source.partition('echo -n "Make temp dir: "')
        self.assertTrue(marker, 'Test boundary must precede packaging/temp-directory operations')
        with tempfile.TemporaryDirectory(prefix='lma-publish-bucket-test-') as directory:
            temp = Path(directory)
            fake = temp / 'aws'
            fake.write_text(FAKE_AWS)
            fake.chmod(0o755)
            calls_path = temp / 'calls.jsonl'
            env = dict(os.environ, PATH=f'{temp}:{os.environ["PATH"]}',
                       MOCK_CALLS=str(calls_path), MOCK_HEAD_ERROR=error)
            result = subprocess.run(
                ['bash', '-c', preflight, script, 'existing-ci-bucket', 'private/prefix/', 'us-east-1'],
                cwd=temp, env=env, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
        return result, calls

    def test_existing_bucket_requires_only_targeted_head(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls = self.run_preflight(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [['s3api', 'head-bucket', '--bucket',
                                         'existing-ci-bucket', '--region', 'us-east-1']])
                self.assertIn('Using existing bucket', result.stdout)

    def test_explicit_missing_bucket_preserves_standalone_creation(self):
        for script in SCRIPTS:
            for error_code in ('404', 'NoSuchBucket', 'NotFound'):
                with self.subTest(script=script, error_code=error_code):
                    result, calls = self.run_preflight(
                        script, f'An error occurred ({error_code}) when calling the HeadBucket operation')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls[1], ['s3', 'mb', 's3://existing-ci-bucket',
                                                '--region', 'us-east-1'])
                    self.assertEqual(calls[2][:2], ['s3api', 'put-bucket-versioning'])
                    self.assertEqual(len(calls), 3)

    def test_other_failures_stop_without_bucket_creation(self):
        errors = ('An error occurred (403) when calling the HeadBucket operation: Forbidden',
                  'An error occurred (AccessDenied) when calling the HeadBucket operation',
                  'An error occurred (500) when calling the HeadBucket operation',
                  'Could not connect to the endpoint URL',
                  'Unable to locate credentials')
        for script in SCRIPTS:
            for error in errors:
                with self.subTest(script=script, error=error):
                    result, calls = self.run_preflight(script, error)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(len(calls), 1)
                    self.assertIn('Cannot access S3 bucket', result.stderr)


if __name__ == '__main__':
    unittest.main()
