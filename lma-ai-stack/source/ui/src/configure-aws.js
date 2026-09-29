import { Amplify } from 'aws-amplify';
import 'aws-amplify/auth/enable-oauth-listener';
import awsExports from './aws-exports';

// Run before importing App: its dependencies create clients at module scope.
// Module initialization runs once, including under React StrictMode.
Amplify.configure(awsExports);

export default awsExports;
