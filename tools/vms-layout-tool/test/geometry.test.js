const test = require('node:test');
const assert = require('node:assert');
const Geo = require('../src/geometry.js');

const B = (x1, y1, x2, y2, type = 'Text') => ({ type, x1, y1, x2, y2 });

test('four text rows on 272x96 are equal and nearly centred', () => {
  const bs = Geo.preset({ W: 272, H: 96, margin: 5, gap: 3, rows: 4, picto: 'none' });
  assert.strictEqual(bs.length, 4);
  assert.ok(bs.every((b) => Geo.bh(b) === Geo.bh(bs[0])));
  const gaps = bs.slice(1).map((b, i) => b.y1 - bs[i].y2 - 1);
  assert.deepStrictEqual(gaps, [3, 3, 3]);
  const issues = Geo.analyse(bs, 272, 96);
  assert.ok(issues.every((i) => i.ok), JSON.stringify(issues));
});

test('pictogram left leaves mirror-image margins and mirrors cleanly', () => {
  const bs = Geo.preset({ W: 272, H: 96, margin: 5, gap: 3, rows: 3, picto: 'left' });
  const pic = bs.find((b) => b.type === 'Graphic');
  assert.strictEqual(pic.x1, 6);
  const copy = bs.map((b) => ({ ...b }));
  Geo.mirror(copy, 272, 96, 'h');
  const pic2 = copy.find((b) => b.type === 'Graphic');
  assert.strictEqual(272 - pic2.x2, pic.x1 - 1);
  Geo.mirror(copy, 272, 96, 'h');
  assert.deepStrictEqual(copy, bs);
});

test('centreInSign equalises margins', () => {
  const bs = [B(53, 1, 269, 23), B(53, 33, 269, 78)];
  Geo.centreInSign(bs, 'h', 272, 96);
  const bb = Geo.bbox(bs);
  assert.ok(Math.abs(bb.x1 - 1 - (272 - bb.x2)) <= 1);
});

test('distribute keeps ends and equalises gaps', () => {
  const bs = [B(5, 5, 100, 23), B(5, 27, 100, 45), B(5, 50, 100, 68), B(5, 73, 100, 91)];
  Geo.distribute(bs, 'v', null);
  const gaps = bs.slice(1).map((b, i) => b.y1 - bs[i].y2 - 1);
  assert.strictEqual(bs[0].y1, 5);
  assert.strictEqual(bs[3].y2, 91);
  assert.ok(Math.max(...gaps) - Math.min(...gaps) <= 1, gaps.join());
});

test('tidy fixes the uneven rows seen in the OMNIA export', () => {
  const bs = [B(5, 5, 268, 23), B(5, 27, 268, 45), B(5, 50, 268, 68), B(5, 73, 268, 91)];
  Geo.tidy(bs, 272, 96);
  const issues = Geo.analyse(bs, 272, 96);
  assert.ok(issues.every((i) => i.ok), JSON.stringify(issues));
});

test('tidy ignores one stray wide box', () => {
  const bs = [B(95, 6, 267, 24), B(95, 28, 267, 46), B(100, 50, 272, 68), B(95, 72, 267, 90)];
  Geo.tidy(bs, 272, 96);
  assert.ok(bs.every((b) => b.x1 === bs[0].x1 && b.x2 === bs[0].x2));
  assert.strictEqual(Geo.bw(bs[0]), 173);
});

test('overlap is reported', () => {
  const issues = Geo.analyse([B(1, 1, 50, 50), B(40, 40, 90, 90)], 272, 96);
  assert.ok(issues.some((i) => i.key === 'overlap'));
});

test('sameSize and align', () => {
  const bs = [B(1, 1, 10, 10), B(20, 5, 40, 30)];
  Geo.sameSize(bs, 'both');
  assert.strictEqual(Geo.bw(bs[1]), 10);
  assert.strictEqual(Geo.bh(bs[1]), 10);
  Geo.align(bs, 'top');
  assert.strictEqual(bs[1].y1, 1);
});
