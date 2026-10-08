"""Private, retained document source contract; no bucket is created by these tests."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class DocumentSourceTests(unittest.TestCase):
    def test_document_source_is_opt_in_private_encrypted_and_retained(self):
        template = yaml.load((ROOT / 'lma-main.yaml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(template['Parameters']['CreateDocumentSourceBucket']['Default'], 'false')
        bucket = template['Resources']['DocumentsBucket']
        self.assertEqual(bucket['Condition'], 'ShouldCreateDocumentSourceBucket')
        self.assertEqual(bucket['DeletionPolicy'], 'Retain')
        self.assertEqual(bucket['UpdateReplacePolicy'], 'Retain')
        props = bucket['Properties']
        self.assertTrue(all(v == 'true' for v in props['PublicAccessBlockConfiguration'].values()))
        self.assertEqual(props['VersioningConfiguration']['Status'], 'Enabled')
        self.assertEqual(props['OwnershipControls']['Rules'][0]['ObjectOwnership'], 'BucketOwnerEnforced')
        self.assertEqual(props['BucketEncryption']['ServerSideEncryptionConfiguration'][0]
                         ['ServerSideEncryptionByDefault']['SSEAlgorithm'], 'AES256')
        self.assertEqual(props['BucketName'], 'allops-lma-documents-${AWS::AccountId}-${AWS::Region}')
        statement = template['Resources']['DocumentsBucketPolicy']['Properties']['PolicyDocument']['Statement'][0]
        self.assertEqual(statement['Effect'], 'Deny')
        self.assertEqual(statement['Condition']['Bool']['aws:SecureTransport'], 'false')

    def test_document_source_is_wired_to_the_knowledge_base_and_assistant(self):
        template = yaml.load((ROOT / 'lma-main.yaml').read_text(), Loader=yaml.BaseLoader)
        expected = ['ShouldCreateDocumentSourceBucket', 'DocumentsBucket', 'BedrockKnowledgeBaseS3BucketName']
        kb = template['Resources']['BEDROCKKB']['Properties']['Parameters']
        self.assertEqual(kb['pKnowledgeBaseBucketName'], expected)
        consumers = [r for r in template['Resources'].values()
                     if r['Type'] == 'AWS::CloudFormation::Stack'
                     and 'BedrockKnowledgeBaseS3BucketName' in r['Properties']['Parameters']]
        self.assertEqual(len(consumers), 1)
        self.assertEqual(consumers[0]['Properties']['Parameters']['BedrockKnowledgeBaseS3BucketName'], expected)
        self.assertIn('ManagedDocumentSource', template['Rules'])


if __name__ == '__main__':
    unittest.main()
