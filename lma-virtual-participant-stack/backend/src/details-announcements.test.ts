import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { DetailsManager } from './details.js';

test('default and previous deployed VP names migrate to allOps LMA without announcements', () => {
    const before = { identity: process.env.LMA_IDENTITY, intro: process.env.INTRO_MESSAGE,
        start: process.env.START_RECORDING_MESSAGE };
    try {
        process.env.INTRO_MESSAGE = 'Legacy hello';
        process.env.START_RECORDING_MESSAGE = 'Legacy started';
        for (const identity of ['', 'LMA ({LMA_USER})', 'allOps LMA']) {
            process.env.LMA_IDENTITY = identity;
            const details = new DetailsManager().details;
            assert.equal(details.scribeIdentity, 'allOps LMA');
            assert.deepEqual(details.introMessages, []);
            assert.deepEqual(details.startMessages, []);
            assert.ok(details.pauseMessages.length);
            assert.ok(details.exitMessages.length);
        }
    } finally {
        for (const [key, value] of [['LMA_IDENTITY', before.identity], ['INTRO_MESSAGE', before.intro],
            ['START_RECORDING_MESSAGE', before.start]]) {
            if (value === undefined) delete process.env[key!];
            else process.env[key!] = value;
        }
    }
});
