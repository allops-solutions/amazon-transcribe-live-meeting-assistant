/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useEffect } from 'react';
import { Hub } from 'aws-amplify/utils';
import awsExports from '../configure-aws';

const useAwsConfig = () => {
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
    return cancel;
  }, []);
  return awsExports;
};

export default useAwsConfig;
