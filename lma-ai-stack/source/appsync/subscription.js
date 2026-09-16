/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export function request() {
  return { payload: null };
}

/**
 * allOps policy: every authenticated user (Admin or User) receives every
 * live update — no Owner/SharedWith subscription filter. See
 * getCall.response.vtl for the rationale.
 *
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function response(ctx) {
  console.debug(`setting up subscription for user ${ctx.identity.username}`);
  return null;
}
