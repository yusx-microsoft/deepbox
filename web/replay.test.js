const test = require('node:test');
const assert = require('node:assert/strict');
const {nearestCheckpointIndex, eventsBetween, normalizeReplay, formatClock} = require('./replay.js');

test('normalizeReplay maps server frame and checkpoint fields', () => {
  const replay = normalizeReplay({
    events: [{frame_id: 8, time: 2, kind: 'output', data: 'b'},
             {frame_id: 7, time: 1, kind: 'output', data: 'a'}],
    checkpoints: [{frame_id: 7, time: 1, screen: 'snapshot'}],
  });
  assert.deepEqual(replay.events.map(e => e.cursor), [7, 8]);
  assert.equal(replay.events[0].type, 'output');
  assert.equal(replay.checkpoints[0].cursor, 7);
  assert.equal(replay.checkpoints[0].serialized_screen, 'snapshot');
});

test('checkpoint seek applies only frames after its durable cursor', () => {
  const checkpoints = [{time: 1, cursor: 4}, {time: 3, cursor: 9}];
  assert.equal(nearestCheckpointIndex(checkpoints, 2), 0);
  const events = [{time: 1, cursor: 4}, {time: 1, cursor: 5}, {time: 2, cursor: 6}];
  assert.deepEqual(eventsBetween(events, 1, 2, 4).map(e => e.cursor), [5, 6]);
});

test('formatClock is stable for replay controls', () => {
  assert.equal(formatClock(0), '0:00');
  assert.equal(formatClock(65.9), '1:05');
});
