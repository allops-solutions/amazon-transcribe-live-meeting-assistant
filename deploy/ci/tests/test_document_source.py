"""Private, retained document source contract; no bucket is created by these tests."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class DocumentSourceTests(unittest.TestCase):
    def test_document_source_is_opt_in_private_encrypted_and_retained(self):
        template = yaml.load((ROOT / 'deploy/ci/document-source.yaml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(template['Parameters']['EnableDocumentsBucket']['Default'], 'false')
        self.assertEqual(set(template['Resources']), {'Documents', 'DocumentsPolicy'})
        bucket = template['Resources']['Documents']
        self.assertEqual(bucket['Condition'], 'Enabled')
        self.assertEqual(bucket['DeletionPolicy'], 'Retain')
        self.assertEqual(bucket['UpdateReplacePolicy'], 'Retain')
        props = bucket['Properties']
        self.assertTrue(all(v == 'true' for v in props['PublicAccessBlockConfiguration'].values()))
        self.assertEqual(props['VersioningConfiguration']['Status'], 'Enabled')
        self.assertEqual(props['OwnershipControls']['Rules'][0]['ObjectOwnership'], 'BucketOwnerEnforced')
        self.assertEqual(props['BucketEncryption']['ServerSideEncryptionConfiguration'][0]
                         ['ServerSideEncryptionByDefault']['SSEAlgorithm'], 'AES256')
        self.assertEqual(props['BucketName'], 'allops-lma-documents-${AWS::AccountId}-${AWS::Region}')
        statement = template['Resources']['DocumentsPolicy']['Properties']['PolicyDocument']['Statement'][0]
        self.assertEqual(statement['Effect'], 'Deny')
        self.assertEqual(statement['Condition']['Bool']['aws:SecureTransport'], 'false')


if __name__ == '__main__':
    unittest.main()
