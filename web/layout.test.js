const test = require('node:test');
const assert = require('node:assert/strict');
const L = require('./layout.js');

test('split is immutable and supports a tmux-style mixed layout', () => {
  const original = L.pane('one');
  const right = L.split(original, 'one', 'two', 'row');
  const lower = L.split(right, 'two', 'three', 'column');
  assert.deepEqual(original, {type:'pane', id:'one'});
  assert.equal(right.type, 'split');
  assert.equal(right.axis, 'row');
  assert.equal(lower.second.axis, 'column');
  assert.deepEqual(L.ids(lower), ['one','two','three']);
  assert.strictEqual(lower.first, original);
  assert.strictEqual(right.second.type, 'pane');
});

test('splitting is bounded, never duplicates identities, and ignores stale targets', () => {
  let tree = L.pane('one');
  tree = L.split(tree, 'one', 'two', 'row');
  tree = L.split(tree, 'two', 'three', 'column');
  tree = L.split(tree, 'one', 'four', 'column');
  assert.strictEqual(L.split(tree, 'four', 'five', 'row'), tree);
  assert.strictEqual(L.split(tree, 'one', 'two', 'row'), tree);
  assert.strictEqual(L.split(tree, 'gone', 'five', 'column'), tree);
  assert.throws(() => L.split(tree, 'one', '<bad>', 'row'), /Invalid pane/);
  assert.throws(() => L.split(tree, 'one', 'five', 'diagonal'), /Invalid split/);
});

test('closing a nested pane collapses only its empty split', () => {
  const left = L.pane('left');
  const initial = L.split(L.split(left, 'left', 'top', 'row'), 'top', 'bottom', 'column');
  const tree = L.remove(initial, 'top');
  assert.strictEqual(tree.first, left);
  assert.deepEqual(tree.second, L.pane('bottom'));
  assert.deepEqual(L.ids(initial), ['left','top','bottom']);
  assert.deepEqual(L.remove(tree, 'left'), L.pane('bottom'));
  assert.equal(L.remove(L.pane('last'), 'last'), null);
  assert.strictEqual(L.remove(tree, 'missing'), tree);
});

test('resizing is local, immutable, bounded, and ignores invalid/stale paths', () => {
  const original = L.split(L.split(L.pane('one'), 'one', 'two', 'row'), 'two', 'three', 'column');
  const tree = L.resize(original, ['second'], 0.7);
  assert.equal(tree.second.ratio, 0.7);
  assert.equal(original.second.ratio, 0.5);
  assert.strictEqual(tree.first, original.first);
  assert.equal(L.resize(tree, [], -1).ratio, L.MIN_RATIO);
  assert.equal(L.resize(tree, [], 2).ratio, L.MAX_RATIO);
  assert.equal(L.resize(tree, [], NaN).ratio, 0.5);
  assert.strictEqual(L.resize(tree, ['first'], 0.4), tree);
  assert.strictEqual(L.resize(tree, ['oops'], 0.4), tree);
  assert.strictEqual(L.at(tree, ['second']), tree.second);
  assert.equal(L.at(tree, ['first','second']), null);
});

test('saved layout is normalized and strips unrelated data', () => {
  const value = {type:'split', axis:'row', ratio:10, token:'not-persisted',
    first:{type:'pane',id:'one',prompt:'not-persisted'}, second:{type:'pane',id:'two'}};
  assert.deepEqual(L.sanitize(value), {type:'split',axis:'row',ratio:L.MAX_RATIO,
    first:L.pane('one'), second:L.pane('two')});
  assert.deepEqual(L.sanitize(JSON.parse(JSON.stringify(L.sanitize(value)))), L.sanitize(value));
});

test('corrupt, oversized, duplicate, or cyclic saved layouts are rejected', () => {
  assert.equal(L.sanitize(null), null);
  assert.equal(L.sanitize({type:'pane',id:'bad" id'}), null);
  assert.equal(L.sanitize({type:'window',id:'one'}), null);
  assert.equal(L.sanitize({type:'split',axis:'row',first:L.pane('one'),second:L.pane('one')}), null);
  const cycle = {type:'split',axis:'row',second:L.pane('one')}; cycle.first = cycle;
  assert.equal(L.sanitize(cycle), null);
  const five = {type:'split',axis:'row',first:L.pane('five'),second:
    L.split(L.split(L.split(L.pane('one'),'one','two','row'),'two','three','row'),'three','four','row')};
  assert.equal(L.sanitize(five), null);
});

test('normalized rectangles follow DFS order and split ratios without mutating the tree', () => {
  let tree = L.split(L.pane('left'), 'left', 'right', 'row');
  tree = L.split(tree, 'right', 'bottom', 'column');
  tree = L.split(tree, 'left', 'lower-left', 'column');
  tree = L.resize(L.resize(tree, [], 0.25), ['second'], 0.75);
  const before = JSON.stringify(tree);
  assert.deepEqual(L.rects(tree), [
    {id:'left', x:0, y:0, width:0.25, height:0.5},
    {id:'lower-left', x:0, y:0.5, width:0.25, height:0.5},
    {id:'right', x:0.25, y:0, width:0.75, height:0.75},
    {id:'bottom', x:0.25, y:0.75, width:0.75, height:0.25},
  ]);
  assert.deepEqual(L.rects(tree).map(rect=>rect.id), L.ids(tree));
  assert.equal(JSON.stringify(tree), before);
  assert.deepEqual(L.rects(L.pane('single')), [{id:'single', x:0, y:0, width:1, height:1}]);
  assert.deepEqual(L.rects(null), []);
});

test('directional neighbors share an edge, choose the nearest center, and do not wrap', () => {
  let tree = L.split(L.pane('left'), 'left', 'top', 'row');
  tree = L.split(tree, 'top', 'bottom', 'column');
  assert.equal(L.neighbor(tree, 'left', 'right'), 'top', 'ties follow DFS order');
  assert.equal(L.neighbor(tree, 'top', 'left'), 'left');
  assert.equal(L.neighbor(tree, 'bottom', 'left'), 'left');
  assert.equal(L.neighbor(tree, 'top', 'down'), 'bottom');
  assert.equal(L.neighbor(tree, 'bottom', 'up'), 'top');
  for(const [id, direction] of [['left','left'], ['left','up'], ['left','down'], ['top','up'], ['bottom','down'], ['top','right']])
    assert.equal(L.neighbor(tree, id, direction), null);
  assert.equal(L.neighbor(L.resize(tree, ['second'], 0.25), 'left', 'right'), 'bottom');
  assert.equal(L.neighbor(L.resize(tree, ['second'], 0.75), 'left', 'right'), 'top');
  tree = L.split(tree, 'left', 'lower-left', 'column');
  assert.equal(L.neighbor(tree, 'left', 'right'), 'top', 'corner-only contact is excluded');
  assert.equal(L.neighbor(tree, 'lower-left', 'right'), 'bottom');
  tree = L.resize(tree, ['second'], 0.75);
  assert.equal(L.neighbor(tree, 'lower-left', 'right'), 'bottom', 'nearest of two overlapping neighbors');
  assert.equal(L.neighbor(tree, 'top', 'left'), 'left');
});

test('neighbors tolerate missing panes and invalid directions and update after collapse', () => {
  const tree = L.split(L.split(L.pane('left'), 'left', 'top', 'row'), 'top', 'bottom', 'column');
  assert.equal(L.neighbor(tree, 'missing', 'right'), null);
  assert.equal(L.neighbor(tree, 'left', 'diagonal'), null);
  assert.equal(L.neighbor(tree, 'left'), null);
  assert.equal(L.neighbor(null, 'left', 'right'), null);
  assert.equal(L.neighbor(L.pane('left'), 'left', 'right'), null);
  const collapsed = L.remove(tree, 'top');
  assert.equal(L.neighbor(collapsed, 'left', 'right'), 'bottom');
  assert.equal(L.neighbor(collapsed, 'top', 'down'), null);
  assert.deepEqual(L.rects(collapsed).map(rect=>rect.id), ['left','bottom']);
});
