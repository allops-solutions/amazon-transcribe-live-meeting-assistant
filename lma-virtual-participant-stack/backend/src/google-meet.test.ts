/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildMeetUrl } from './google-meet.js';
import { MEETING_HOST_PATTERNS, arrivedAtExpectedHost } from './meeting-navigation.js';

test('buildMeetUrl: full https URL is used as-is', () => {
    assert.equal(buildMeetUrl('https://meet.google.com/abc-defg-hij'), 'https://meet.google.com/abc-defg-hij');
});

test('buildMeetUrl: URL with query string is preserved', () => {
    assert.equal(
        buildMeetUrl('https://meet.google.com/abc-defg-hij?authuser=0'),
        'https://meet.google.com/abc-defg-hij?authuser=0',
    );
});

test('buildMeetUrl: bare host path gets https prefix', () => {
    assert.equal(buildMeetUrl('meet.google.com/abc-defg-hij'), 'https://meet.google.com/abc-defg-hij');
});

test('buildMeetUrl: meeting code becomes a meet.google.com URL', () => {
    assert.equal(buildMeetUrl('abc-defg-hij'), 'https://meet.google.com/abc-defg-hij');
});

test('buildMeetUrl: whitespace inside the code is stripped', () => {
    assert.equal(buildMeetUrl(' abc-defg-hij '), 'https://meet.google.com/abc-defg-hij');
    assert.equal(buildMeetUrl('abc - defg - hij'), 'https://meet.google.com/abc-defg-hij');
});

test('MEETING_HOST_PATTERNS.meet matches meet.google.com only', () => {
    assert.ok(arrivedAtExpectedHost('https://meet.google.com/abc-defg-hij', MEETING_HOST_PATTERNS.meet));
    assert.ok(!arrivedAtExpectedHost('https://accounts.google.com/signin', MEETING_HOST_PATTERNS.meet));
    assert.ok(!arrivedAtExpectedHost('about:blank', MEETING_HOST_PATTERNS.meet));
});
