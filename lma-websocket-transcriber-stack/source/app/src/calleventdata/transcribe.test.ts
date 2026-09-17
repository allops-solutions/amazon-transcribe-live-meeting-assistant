/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { MAX_AUDIO_CHUNK_BYTES, frameAudioChunk } from './transcribe';

test('the frame cap matches the Virtual Participant scribe', () => {
    assert.equal(MAX_AUDIO_CHUNK_BYTES, 16 * 1024);
});

test('a chunk within the limit is passed through without copying', () => {
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES, 1);
    const frames = frameAudioChunk(chunk);

    assert.equal(frames.length, 1);
    assert.equal(frames[0], chunk);
});

test('an oversized chunk is split into frames within the limit', () => {
    // A MicroVM ASR acquisition delays the Transcribe consumer by seconds, so the
    // first read can carry the whole backlog: 20s of 16 kHz mono PCM16 here.
    const chunk = Buffer.alloc(16000 * 2 * 20, 9);
    const frames = frameAudioChunk(chunk);

    assert.ok(frames.length > 1);
    for (const frame of frames) {
        assert.ok(
            frame.length <= MAX_AUDIO_CHUNK_BYTES,
            `frame of ${frame.length} bytes exceeds the limit`
        );
    }
});

test('splitting loses no audio and preserves order', () => {
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES * 3 + 517);
    for (let i = 0; i < chunk.length; i += 1) {
        chunk[i] = i % 251;
    }

    const rejoined = Buffer.concat(frameAudioChunk(chunk));

    assert.equal(rejoined.length, chunk.length);
    assert.ok(rejoined.equals(chunk));
});

test('an empty chunk yields one empty frame rather than nothing', () => {
    assert.deepEqual(frameAudioChunk(Buffer.alloc(0)), [Buffer.alloc(0)]);
});

// --- Per-meeting language mode ---------------------------------------------

import { languageParamsFor, transcribeLanguageModeFor } from './transcribe';

test('a well-formed per-meeting language mode overrides the deployment default', () => {
    assert.equal(transcribeLanguageModeFor({ transcribeLanguageMode: 'bs-BA' }, 'en-US'), 'bs-BA');
    assert.equal(
        transcribeLanguageModeFor({ transcribeLanguageMode: 'identify-language' }, 'en-US'),
        'identify-language'
    );
    assert.equal(
        transcribeLanguageModeFor({ transcribeLanguageMode: 'identify-multiple-languages' }, 'en-US'),
        'identify-multiple-languages'
    );
});

test('an absent, blank or malformed per-meeting language mode falls back to the deployment default', () => {
    assert.equal(transcribeLanguageModeFor({}, 'identify-multiple-languages'), 'identify-multiple-languages');
    assert.equal(transcribeLanguageModeFor({ transcribeLanguageMode: '' }, 'en-US'), 'en-US');
    assert.equal(transcribeLanguageModeFor({ transcribeLanguageMode: '   ' }, 'en-US'), 'en-US');
    assert.equal(transcribeLanguageModeFor({ transcribeLanguageMode: 'english' }, 'en-US'), 'en-US');
    assert.equal(transcribeLanguageModeFor({ transcribeLanguageMode: 'en-US; drop' }, 'en-US'), 'en-US');
});

test('an explicit language code sets only LanguageCode', () => {
    assert.deepEqual(languageParamsFor('hr-HR', 'en-US, bs-BA', 'bs-BA'), { LanguageCode: 'hr-HR' });
});

test('identify-language carries the deployment language options and preferred language', () => {
    assert.deepEqual(languageParamsFor('identify-language', 'en-US, bs-BA', 'bs-BA'), {
        IdentifyLanguage: true,
        LanguageOptions: 'en-US,bs-BA',
        PreferredLanguage: 'bs-BA',
    });
});

test('identify-multiple-languages omits PreferredLanguage when it is None', () => {
    assert.deepEqual(languageParamsFor('identify-multiple-languages', 'en-US, es-US', 'None'), {
        IdentifyMultipleLanguages: true,
        LanguageOptions: 'en-US,es-US',
    });
});
