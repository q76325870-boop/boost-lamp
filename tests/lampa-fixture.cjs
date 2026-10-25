const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../plugin/boosty.js'), 'utf8');

module.exports = function fixture({external = false, inline = false, storage = new Map()} = {}) {
  let clock = 20000, current, selected, entry, component, controller = '', timer = 0;
  const xhrs = [], nodes = [], controllers = {}, notices = [], updates = [];
  class Element {
    constructor(html = '') { this.classes = (html.match(/class="([^"]*)"/)?.[1] || '').split(' '); this.children = []; this.events = {}; this.value = ''; this.attrs = {}; nodes.push(this); }
    text(value) { if (value === undefined) return this.value; this.value = value; return this; }
    append(child) { if (child instanceof Element) this.children.push(child); return this; }
    appendTo() { return this; }
    empty() { this.children = []; return this; }
    remove() { this.removed = true; return this; }
    addClass(value) { this.classes.push(value); return this; }
    attr(key, value) { this.attrs[key] = value; return this; }
    on(event, callback) { this.events[event] = callback; return this; }
    first() { return this; }
    get length() { return 1; }
    find(selector) { return all(this, n => n.classes.includes(selector.slice(1)))[0] || {length: 0, first() { return this; }}; }
    trigger(event) { this.events[event]?.call(this); return this; }
  }
  const menu = new Element();
  function $(value) { return value instanceof Element ? value : value.startsWith('<') ? new Element(value) : menu; }
  function listener() {
    const events = new Map();
    return {
      follow(name, callback) { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(callback); },
      remove(name, callback) { events.get(name)?.delete(callback); },
      emit(name, value) { [...(events.get(name) || [])].forEach(fn => fn(value)); },
      count() { return [...events.values()].reduce((n, set) => n + set.size, 0); }
    };
  }
  class XHR {
    constructor() { this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(body) { this.body = body ? JSON.parse(body) : null; xhrs.push(this); }
    respond(value, status = 200) { this.responded = true; this.status = status; this.responseText = JSON.stringify(value); this.onload(); }
    abort() { this.aborted = true; }
  }
  const L = {
    Storage: {get(key, fallback) { return key.startsWith('boosty_device_') ? 'test-device' : storage.get(key) ?? fallback; }, set(key, value) { storage.set(key, value); }, field() { return 'continue'; }},
    Component: {add(name, ctor) { component = ctor; }},
    Activity: {push(object) {
      if (current) { current.activity.active = false; current.pause(); }
      const instance = new component(object);
      instance.activity = {active: true, loader() {}, toggle() { if (this.active) instance.start(); }};
      current = instance; instance.create(); instance.start(); return instance;
    }, backward() {}},
    Scroll: function () { const root = new Element(); this.render = () => root; this.append = x => root.append(x); this.minus = this.update = () => {}; this.destroy = () => root.remove(); },
    Controller: {add(name, value) { controllers[name] = value; }, toggle(name) { controller = name; controllers[name]?.toggle(); }, collectionSet(value) { this.collection = value; }, collectionFocus(value) { this.focused = value; }},
    Select: {show(value) { selected = value; }, hide() {}},
    Noty: {show(text) { notices.push(text); }},
    Timeline: {update(value) { updates.push({...value}); }},
    Player: {listener: listener(), play(value) { entry = value; if (!external) this.listener.emit('start', value); }, playlist() {}, close() { this.listener.emit('destroy', {}); }},
    PlayerVideo: {listener: listener()}
  };
  const script = {src: inline ? '' : 'http://server.test:19118/boosty.js'};
  const context = {Lampa: L, appready: true, $: $, jQuery: $, XMLHttpRequest: XHR, Navigator: {canmove: () => false, move() {}},
    document: {currentScript: script, getElementsByTagName: () => [script]},
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
    setTimeout: () => ++timer, clearTimeout() {}, Date: {now: () => clock}, console};
  context.window = context;
  vm.runInNewContext(source, context);
  function all(root, predicate) { return (predicate(root) ? [root] : []).concat(root.children.flatMap(node => all(node, predicate))); }
  return {
    L, storage, xhrs, nodes, notices, updates,
    down() { controllers.content.down(); },
    registered: () => !!component,
    open(action = 'posts') { return L.Activity.push({action, author: 'author', title: action}); },
    current: () => current, entry: () => entry,
    nextRequest(method, suffix) { return xhrs.find(x => !x.responded && x.method === method && x.url.includes(suffix)); },
    respondCatalog(cursor = null) { this.nextRequest('GET', '/posts').respond({items: [{author_id: 'author', post_id: 'post', videos: [{video_id: 'video', title: 'Film', qualities: ['full_hd'], duration_seconds: 1000}]}], next_cursor: cursor}); },
    cards(instance = current) { return all(instance.render(), n => n.classes.includes('boosty-card')); },
    button(text, instance = current) { return all(instance.render(), n => n.value === text)[0]; },
    launch(position = 0, duration = 1000) {
      this.cards()[0].trigger('hover:enter'); selected.onSelect({value: 'full_hd'});
      this.nextRequest('POST', '/playbacks').respond({playback_id: 'play', url: 'http://server.test:19118/boosty/media/play/video.mp4', key: 'video-key', title: 'Film', position_seconds: position, duration_seconds: duration, completed: false});
    },
    update(position, duration = 1000) { clock += 16000; L.PlayerVideo.listener.emit('timeupdate', {current: position, duration}); },
    finish() { L.Player.close(); },
    focusOther() { current.activity.active = false; current.pause(); L.Controller.collection = 'other-page'; controller = 'other'; },
    focus: () => ({controller, collection: L.Controller.collection})
  };
};
