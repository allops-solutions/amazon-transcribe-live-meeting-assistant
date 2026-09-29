/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useState, useEffect } from 'react';
import { Amplify } from 'aws-amplify';
import 'aws-amplify/auth/enable-oauth-listener';
import { Hub } from 'aws-amplify/utils';
import awsExports from '../aws-exports';

const useAwsConfig = () => {
  const [awsConfig, setAwsConfig] = useState();
  useEffect(() => {
    const cancel = Hub.listen('auth', ({ payload }) => {
      if (payload.event === 'signInWithRedirect') {
        const target = sessionStorage.getItem('lma-sso-return-to');
        sessionStorage.removeItem('lma-sso-return-to');
        if (target?.startsWith('/') && !target.startsWith('//')) {
          window.location.hash = target;
        }
      }
    });
    Amplify.configure(awsExports);
    setAwsConfig(awsExports);
    return cancel;
  }, []);
  return awsConfig;
};

export default useAwsConfig;
