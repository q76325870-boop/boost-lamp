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
  const f = fixture(); f.open(); f.respondCatalog();
  const first = f.cards()[0];
  const home = f.button('Авторы');
  f.focusNode(home);
  f.down();
  assert.equal(f.L.Controller.focused, first);
});

test('home opens authors with distinct tabs, avatar cards and author cover', () => {
  const f = fixture(); f.open('home');
  assert.equal(f.cards().length,0);
  for(const title of ['Авторы','Новые видео','Продолжить']) assert.ok(f.button(title));
  assert.equal(f.button('Главная'),undefined);
  assert.equal(f.button('Подключение'),undefined);
  f.nextRequest('GET','/authors').respond({items:[{author_id:'creator',title:'Creator',description:'Video essays',image_url:'http://server.test:19118/boosty/images/avatar',cover_url:'http://server.test:19118/boosty/images/cover'}],next_cursor:null});
  const card=f.cards()[0];
  assert.ok(card.classes.includes('boosty-author'));
  assert.equal(card.find('img').attrs.src,'http://server.test:19118/boosty/images/avatar');
  card.trigger('hover:enter');
  assert.match(f.nextRequest('GET','/posts').url,/author=creator/);
  assert.ok(f.current().render().find('.boosty-cover').length);
  assert.equal(f.current().render().find('.boosty-cover').find('img').attrs.src,'http://server.test:19118/boosty/images/cover');
});

test('remote up from cards reaches section tabs when geometry has no neighbour', () => {
  const f=fixture(); f.open(); f.respondCatalog();
  f.cards()[0].addClass('focus').trigger('hover:focus');
  f.up();
  assert.equal(f.L.Controller.focused,f.button('Авторы'));
  assert.equal(f.focus().controller,'content');
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

test('vertical levels override geometric shortcuts and remember card and tab', () => {
  const f=fixture(); f.open(); f.respondCatalog();
  const card=f.cards()[0], tab=f.button('Новые видео');
  f.focusNode(tab); f.down();
  assert.equal(f.L.Controller.focused,card);
  f.neighbour('up',f.button('Продолжить')); // The geometric candidate is outside the grid.
  f.up();
  assert.equal(f.L.Controller.focused,tab);
  f.up(); assert.equal(f.focus().controller,'head');
  f.resume(); assert.equal(f.L.Controller.focused,tab);
  f.down(); assert.equal(f.L.Controller.focused,card);
});

test('grid neighbours remain reachable; horizontal movement cannot jump levels', () => {
  const f=fixture(); f.open();
  f.nextRequest('GET','/posts').respond({items:[{author_id:'a',post_id:'p',videos:[{video_id:'1',title:'One'},{video_id:'2',title:'Two'}]}],next_cursor:null});
  const [first,second]=f.cards();
  f.focusNode(second); f.neighbour('up',first); f.up();
  assert.equal(f.L.Controller.focused,first);
  f.neighbour('right',f.button('Продолжить')); f.right();
  assert.equal(f.L.Controller.focused,first);
  f.neighbour('down',second); f.down();
  assert.equal(f.L.Controller.focused,second);
  f.neighbour('up',f.button('Авторы')); f.up(); f.down();
  assert.equal(f.L.Controller.focused,second);
});

test('empty catalog keeps tabs focused and the upper panel can return', () => {
  const f=fixture(); f.open('feed');
  f.nextRequest('GET','/feed').respond({items:[],next_cursor:null});
  assert.equal(f.L.Controller.focused,f.button('Новые видео'));
  f.focusNode(f.button('Новые видео')); f.down();
  assert.equal(f.L.Controller.focused,f.button('Новые видео'));
  f.up(); assert.equal(f.focus().controller,'head');
  f.resume(); assert.equal(f.L.Controller.focused,f.button('Новые видео'));
});

test('connection disappears immediately after pairing and returns for invalid binding', () => {
  const f=fixture({connected:false}); f.open('pair');
  assert.ok(f.button('Подключение'));
  f.nextRequest('POST','/pairing').respond({code:'123456',id:'demo',secret:'demo'});
  f.tick(); f.nextRequest('GET','/pairing/demo').respond({token:'demo-device'});
  assert.equal(f.button('Подключение'),undefined);
  assert.equal(f.L.Controller.focused,f.button('Открыть Boosty'));
  f.button('Открыть Boosty').trigger('hover:enter');
  assert.equal(f.button('Подключение'),undefined);
  f.nextRequest('GET','/authors').respond({code:'DEVICE_REQUIRED'},401);
  assert.ok(f.button('Подключение'));
  assert.ok(f.button('Привязать телевизор'));
});


test('lost binding during playback offers focused pairing and keeps paused pages inactive', () => {
  const f=fixture(); f.open(); f.respondCatalog(); f.selectQuality();
  f.nextRequest('POST','/playbacks').respond({code:'DEVICE_REQUIRED'},401);
  assert.ok(f.button('Подключение'));
  assert.equal(f.cards().length,0);
  assert.equal(f.L.Controller.focused,f.button('Привязать телевизор'));
  f.button('Подключение').trigger('hover:enter');
  assert.ok(f.nextRequest('POST','/pairing'));
  const paused=fixture(); paused.open(); paused.respondCatalog(); paused.selectQuality(); paused.focusOther();
  paused.nextRequest('POST','/playbacks').respond({code:'DEVICE_REQUIRED'},401);
  assert.deepEqual(paused.focus(),{controller:'other',collection:'other-page'});
});

test('Back after pairing refreshes the saved page and recovery can reconnect again', () => {
  const f=fixture({connected:false}); const original=f.open('home');
  f.button('Подключение').trigger('hover:enter');
  f.nextRequest('POST','/pairing').respond({code:'123456',id:'demo',secret:'demo'});
  f.tick(); f.nextRequest('GET','/pairing/demo').respond({token:'demo-device'});
  f.L.Activity.backward();
  assert.equal(f.current(),original);
  assert.equal(f.button('Подключение'),undefined);
  assert.equal(f.button('Привязать телевизор'),undefined);
  assert.ok(f.nextRequest('GET','/authors'));
  f.nextRequest('GET','/authors').respond({code:'DEVICE_REQUIRED'},401);
  assert.equal(f.L.Controller.focused,f.button('Привязать телевизор'));
  f.button('Подключение').trigger('hover:enter');
  assert.ok(f.nextRequest('POST','/pairing'));
});

test('remembered non-active tab works with both focus event orders', () => {
  for(const focusEventFirst of [true,false]) {
    const f=fixture({focusEventFirst}); f.open(); f.respondCatalog();
    const tab=f.button('Новые видео'); f.focusNode(tab); f.down(); f.up();
    assert.equal(f.L.Controller.focused,tab);
  }
});


test('late unauthorized playback or catalog cannot erase a newer binding', () => {
  for(const playback of [true,false]) {
    const f=fixture(); f.open();
    if(playback) { f.respondCatalog(); f.selectQuality(); }
    const stale=playback?f.nextRequest('POST','/playbacks'):f.nextRequest('GET','/posts');
    f.focusOther();
    const key='boosty_device_v1_http://server.test:19118'; f.L.Storage.set(key,'new-device');
    stale.respond({code:'DEVICE_REQUIRED'},401);
    assert.equal(f.L.Storage.get(key,''),'new-device');
    assert.deepEqual(f.focus(),{controller:'other',collection:'other-page'});
    assert.equal(f.button('Подключение'),undefined);
  }
});


test('long press selects an Android player for one launch and preserves resume', () => {
  const f=fixture(); f.open(); f.respondCatalog();
  f.L.Storage.set('player','inner');
  f.cards()[0].trigger('hover:long');
  assert.equal(f.selection().title,'Плеер для этого видео');
  assert.deepEqual(Array.from(f.selection().items,x=>x.value),['inner','android']);
  assert.equal(f.nextRequest('POST','/playbacks'),undefined);
  f.select('android'); assert.equal(f.selection().title,'Качество видео');
  f.select('full_hd'); f.respondPlayback(930);
  assert.equal(f.playedPlayers[0],'android');
  assert.equal(f.entry().timeline.time,930); assert.equal(f.entry().timeline.percent,93);
  assert.deepEqual(f.runasCalls,['android','']);
  assert.equal(f.L.Storage.get('player'),'inner');
  f.selectQuality(); f.respondPlayback();
  assert.equal(f.playedPlayers[1],'inner');
});

test('explicit inner player overrides external default without changing it', () => {
  const f=fixture({external:true}); f.open(); f.respondCatalog();
  f.L.Storage.set('player','android'); f.cards()[0].trigger('hover:long');
  f.select('inner'); f.select('full_hd'); f.respondPlayback(930);
  assert.equal(f.playedPlayers[0],'inner');
  assert.equal(f.entry().timeline.time,930); assert.ok(f.entry().timeline.percent<90);
  f.selectQuality(); f.respondPlayback(); assert.equal(f.playedPlayers[1],'android');
  assert.equal(f.L.Storage.get('player'),'android');
});

test('player choice is available in Continue and absent from author cards', () => {
  const f=fixture(); f.open('continue');
  f.nextRequest('GET','/continue').respond({items:[{title:'Film',author_id:'a',post_id:'p',video_id:'v',qualities:['high'],position_seconds:42}],next_cursor:null});
  f.cards()[0].trigger('hover:long'); assert.equal(f.selection().title,'Плеер для этого видео');
  f.select('android'); assert.deepEqual(Array.from(f.selection().items,x=>x.value),['high']);
  f.select('high'); assert.equal(f.nextRequest('POST','/playbacks').body.quality,'high');
  const authors=fixture(); authors.open('authors');
  authors.nextRequest('GET','/authors').respond({items:[{author_id:'a',title:'Author'}],next_cursor:null});
  authors.cards()[0].trigger('hover:long'); assert.equal(authors.selection(),undefined);
});

test('canceling either selector restores the card without creating playback', () => {
  const f=fixture(); f.open(); f.respondCatalog(); const card=f.cards()[0];
  f.focusNode(card);
  card.trigger('hover:long'); f.selection().onBack(); assert.equal(f.L.Controller.focused,card);
  card.trigger('hover:long'); f.select('android'); f.selection().onBack();
  assert.equal(f.L.Controller.focused,card); assert.equal(f.nextRequest('POST','/playbacks'),undefined);
});

test('browser menu does not advertise Android player and a failed launch resets override', () => {
  const browser=fixture({android:false}); browser.open(); browser.respondCatalog(); browser.cards()[0].trigger('hover:long');
  assert.deepEqual(Array.from(browser.selection().items,x=>x.value),['inner']);
  const f=fixture(); f.open(); f.respondCatalog(); f.cards()[0].trigger('hover:long'); f.select('android'); f.select('full_hd');
  f.L.Player.play=()=>{throw new Error('native launch failed');};
  f.respondPlayback();
  assert.deepEqual(f.runasCalls,['android','']);
  assert.equal(f.L.Player.listener.count()+f.L.PlayerVideo.listener.count(),0);
  assert.ok(f.nextRequest('DELETE','/playbacks/'));
  assert.equal(f.nextRequest('PUT','/progress/'),undefined);
  assert.match(f.notices.at(-1),/плеер/);
});
