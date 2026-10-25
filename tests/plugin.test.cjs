const test = require('node:test');
const assert = require('node:assert/strict');
const plugin = require('../plugin/boosty.js');
const fixture = require('./lampa-fixture.cjs');

test('server origin comes from plugin URL, without path or secrets', () => {
  assert.equal(plugin.origin('http://192.168.1.10:19118/boosty.js?v=1'), 'http://192.168.1.10:19118');
  assert.throws(() => plugin.origin('javascript:alert(1)'));
  assert.throws(() => plugin.origin('https://user:secret@example.com/boosty.js'));
});

test('progress never contains NaN, negative time or time beyond duration', () => {
  assert.equal(plugin.progress(NaN, 100), null);
  assert.equal(plugin.progress(-1, 100), null);
  assert.deepEqual(plugin.progress(110, 100), {position_seconds: 100, duration_seconds: 100, completed: true});
  assert.deepEqual(plugin.progress(42, 0), {position_seconds: 42, duration_seconds: null, completed: false});
});

test('API errors remain visible and response text is not used as HTML', () => {
  assert.match(plugin.message('AUTH_REQUIRED'), /аккаунт/i);
  assert.match(plugin.message('ACCESS_DENIED'), /доступ/i);
  assert.match(plugin.message('<script>bad</script>'), /ошибка/i);
});

test('API sends device credentials only as a header and cancellation stops callbacks', () => {
  let xhr;
  class FakeXHR {
    constructor() { xhr = this; this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(k, v) { this.headers[k] = v; }
    send(body) { this.body = body; }
    abort() { this.onabort?.(); }
  }
  let callbacks = 0;
  const cancel = plugin.request(FakeXHR, 'http://server:19118', 'device-secret', 'GET', '/authors', null, () => callbacks++, () => callbacks++);
  assert.equal(xhr.headers.Authorization, 'Bearer device-secret');
  assert.equal(xhr.url, 'http://server:19118/boosty/v1/authors');
  assert.ok(!xhr.url.includes('device-secret'));
  cancel();
  xhr.status = 200; xhr.responseText = '{}'; xhr.onload();
  assert.equal(callbacks, 0);
});

test('successful API call invokes callback once and timeout reports a retryable failure', () => {
  let xhr;
  function FakeXHR() { xhr = this; this.open = () => {}; this.setRequestHeader = () => {}; this.send = () => {}; this.abort = () => {}; }
  let results = [];
  plugin.request(FakeXHR, 'http://server:19118', '', 'GET', '/status', null, x => results.push(x), x => results.push(x));
  xhr.ontimeout(); xhr.status = 200; xhr.responseText = '{}'; xhr.onload();
  assert.deepEqual(results, ['SERVER_UNAVAILABLE']);
});

test('internal playback serializes progress and preserves final backwards seek', () => {
  const f = fixture(); f.open(); f.respondCatalog(); f.launch();
  f.update(700); f.update(800); f.update(500); f.finish();
  const puts = () => f.xhrs.filter(x => x.method === 'PUT');
  assert.equal(puts().length, 1);
  puts()[0].respond({ok:true});
  assert.equal(puts().length, 2);
  assert.equal(puts()[1].body.position_seconds, 500);
  puts()[1].respond({ok:true});
  assert.equal(f.L.Player.listener.count() + f.L.PlayerVideo.listener.count(), 0);
  f.update(900);
  assert.equal(puts().length, 2);
});

test('external player callback saves final position and detaches listeners', () => {
  const f = fixture({external:true}); f.open(); f.respondCatalog(); f.launch(100);
  f.entry().timeline.handler(45,450,1000);
  const save = f.nextRequest('PUT','/progress/');
  assert.equal(save.body.position_seconds, 450);
  save.respond({ok:true});
  assert.equal(f.L.Player.listener.count() + f.L.PlayerVideo.listener.count(),0);
  const count = f.xhrs.length;
  f.entry().timeline.handler(50,500,1000);
  f.update(600);
  assert.equal(f.xhrs.length,count);
});

test('internal Lampa can resume an unfinished video after ninety percent', () => {
  const f = fixture(); f.open(); f.respondCatalog(); f.launch(930);
  const timeline = f.entry().timeline;
  // The native timeline uses percent < 90 as its automatic-seek gate.
  const target = timeline.time > 10 && timeline.percent < 90 ? timeline.time : 0;
  assert.equal(target,930);
  const external = fixture({external:true}); external.open(); external.respondCatalog(); external.launch(930);
  assert.equal(external.entry().timeline.percent,93);
});

test('late results from a paused page cannot steal focus', () => {
  const f = fixture(); f.open(); f.focusOther(); f.respondCatalog();
  assert.deepEqual(f.focus(),{controller:'other',collection:'other-page'});
});

test('remote down reaches a sparse card row from the header outside its horizontal bounds', () => {
  const f = fixture(); f.open('home');
  const first = f.cards()[0];
  assert.equal(f.L.Controller.focused, first);
  const home = f.button('Главная');
  home.addClass('focus');
  home.trigger('hover:focus');
  f.L.Controller.focused = home;
  f.down();
  assert.equal(f.L.Controller.focused, first);
});

test('cached inline script registers using the last validated server origin', () => {
  const initial = fixture(); assert.equal(initial.registered(),true);
  const cached = fixture({inline:true,storage:initial.storage});
  assert.equal(cached.registered(),true);
  cached.open();
  assert.match(cached.nextRequest('GET','/posts').url,/^http:\/\/server\.test:19118\//);
});

test('forty catalog pages do not accumulate cards or completed cancel callbacks', () => {
  const f = fixture(); const screen=f.open();
  for(let i=0;i<40;i++) {
    f.respondCatalog('cursor-'+i);
    assert.equal(f.cards().length,1);
    const next=f.button('Следующая страница') || f.button('Ещё видео');
    assert.ok(next);
    if(i<39) next.trigger('hover:enter');
  }
  f.button('Предыдущая страница').trigger('hover:enter');
  assert.match(f.nextRequest('GET','/posts').url,/cursor=cursor-37$/);
  f.respondCatalog('cursor-38');
  screen.destroy();
  assert.equal(f.xhrs.filter(x=>x.responded && x.aborted).length,0);
});
