(function (root) {
    'use strict';
    function origin(url) {
        var match = /^(https?:\/\/[^\/\s?#]+)(?:[\/?#]|$)/i.exec(url || '');
        if (!match || match[1].indexOf('@') !== -1) throw new Error('Invalid server');
        return match[1].replace(/\/$/, '');
    }
    function progress(position, duration) {
        position = Number(position); duration = Number(duration);
        if (!isFinite(position) || position < 0) return null;
        if (!isFinite(duration) || duration <= 0) duration = null;
        if (duration) position = Math.min(position, duration);
        return {position_seconds: position, duration_seconds: duration, completed: !!(duration && position / duration >= 0.95)};
    }
    var messages = {
        AUTH_REQUIRED: 'Подключите аккаунт Boosty на компьютере.', DEVICE_REQUIRED: 'Нужно привязать этот телевизор.',
        ACCESS_DENIED: 'У аккаунта нет доступа к этому видео.', SERVER_UNAVAILABLE: 'Сервер недоступен. Проверьте, включён ли он.',
        UPSTREAM_UNAVAILABLE: 'Boosty временно недоступен. Попробуйте ещё раз.', RATE_LIMITED: 'Boosty просит подождать. Повторите позже.',
        UNSUPPORTED_MEDIA: 'Нет готового MP4-видео для этого качества.', PLAYBACK_EXPIRED: 'Ссылка истекла. Откройте видео заново.',
        MEDIA_CHANGED: 'Источник обновился. Откройте видео заново с сохранённой позиции.', PAIRING_EXPIRED: 'Код истёк. Получите новый код.'
    };
    function message(code) { return messages[code] || 'Произошла ошибка. Повторите действие.'; }
    function request(XHR, base, token, method, path, body, success, failure, extra) {
        var xhr = new XHR(), ended = false;
        function fail(code) { if (!ended) { ended = true; failure(code); } }
        xhr.open(method, base + '/boosty/v1' + path, true);
        xhr.timeout = 25000;
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        if (body !== null) xhr.setRequestHeader('Content-Type', 'application/json');
        Object.keys(extra || {}).forEach(function (key) { xhr.setRequestHeader(key, extra[key]); });
        xhr.onerror = xhr.ontimeout = function () { fail('SERVER_UNAVAILABLE'); };
        xhr.onload = function () {
            if (ended) return;
            var value;
            try { value = JSON.parse(xhr.responseText); } catch (e) { fail('SERVER_UNAVAILABLE'); return; }
            if (xhr.status < 200 || xhr.status >= 300) { fail(value.code || 'UNKNOWN'); return; }
            ended = true; success(value);
        };
        xhr.send(body === null ? null : JSON.stringify(body));
        return function () { ended = true; xhr.abort(); };
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {origin: origin, progress: progress, message: message, request: request};
        return;
    }
    if (root.__boostyLampaLoaded) return;
    root.__boostyLampaLoaded = true;
    var script = document.currentScript, scripts, i, base;
    if (!script || !script.src) {
        scripts = document.getElementsByTagName('script');
        for (i = scripts.length - 1; i >= 0; i--) if (/\/boosty\.js(?:\?|$)/.test(scripts[i].src)) { script = scripts[i]; break; }
    }
    try { base = origin(script && script.src); }
    catch (e) {
        try { base = origin(root.localStorage.getItem('boosty_server_v1')); }
        catch (ignored) { root.__boostyLampaLoaded = false; return; }
    }
    // Lampa can restore cached plugins as inline scripts without a src.
    try { root.localStorage.setItem('boosty_server_v1', base); } catch (storageUnavailable) {}

    function init() {
        if (root.__boostyLampaReady) return;
        root.__boostyLampaReady = true;
        var L = root.Lampa, $ = root.jQuery || root.$, active = null;
        var tokenKey = 'boosty_device_v1_' + base;
        function call(method, path, body, yes, no, extra) {
            var token = L.Storage.get(tokenKey, '');
            return request(root.XMLHttpRequest, base, token, method, path, body, yes, function (code) {
                if (code === 'DEVICE_REQUIRED' && token !== L.Storage.get(tokenKey, '')) return;
                if (no) no(code); else L.Noty.show(message(code));
            }, extra);
        }
        function push(action, title, author, profile) { L.Activity.push({component: 'boosty', action: action, title: title || 'Boosty', author: author, profile: profile}); }
        function clock(seconds) {
            seconds = Math.max(0, Math.floor(Number(seconds) || 0));
            return Math.floor(seconds / 60) + ':' + ('0' + seconds % 60).slice(-2);
        }
        var qualityNames = {ultra_hd: '2160p', quad_hd: '1440p', full_hd: '1080p', high: '720p', medium: '480p', low: '360p', lowest: '240p', tiny: '144p'};
        function launch(video, quality, onBack, onError) {
            call('POST', '/playbacks', {author_id: video.author_id, post_id: video.post_id, video_id: video.video_id, quality: quality}, function (item) {
                if (active) active.finish();
                var current = progress(item.completed ? 0 : item.position_seconds, item.duration_seconds) || progress(0, 0);
                var lastSent = 0, internal = false, finished = false, warned = false, saving = false, queued = null;
                var hash = 'boosty_' + item.key;
                var timeline = {hash: hash, time: current.position_seconds, duration: current.duration_seconds || 0,
                    percent: current.duration_seconds ? current.position_seconds / current.duration_seconds * 100 : 0};
                if (L.Timeline && L.Timeline.update) L.Timeline.update(timeline);
                function drain() {
                    if (saving || !queued) return;
                    var value = queued; queued = null; saving = true;
                    function done(failed) {
                        if (failed && !warned) L.Noty.show('Не удалось сохранить позицию на сервере.');
                        warned = failed; saving = false; drain();
                    }
                    call('PUT', '/progress/' + item.key, value, function () { done(false); }, function () { done(true); });
                }
                function save(force) {
                    if (!current || (!force && Date.now() - lastSent < 15000)) return;
                    lastSent = Date.now();
                    queued = {position_seconds: current.position_seconds, duration_seconds: current.duration_seconds, completed: current.completed};
                    drain();
                }
                function finish() {
                    if (finished) return;
                    finished = true; save(true);
                    L.Player.listener.remove('start', started);
                    L.Player.listener.remove('destroy', destroyed);
                    L.PlayerVideo.listener.remove('timeupdate', updated);
                    L.PlayerVideo.listener.remove('ended', ended);
                    if (active && active.finish === finish) active = null;
                    call('DELETE', '/playbacks/' + item.playback_id, null, function () {}, function () {});
                }
                function started(data) {
                    if (data.url !== item.url) { finish(); return; }
                    internal = true;
                    // Native Lampa gates automatic seek at 90%; keep the exact time and its start/ask preference.
                    if (!item.completed && data.timeline && data.timeline.percent >= 90) data.timeline.percent = 89.999;
                }
                function destroyed() { if (internal) { finish(); onBack(); } }
                function updated(data) { if (internal && !finished) { current = progress(data.current, data.duration); save(false); } }
                function ended() { if (internal && current) { current.completed = true; save(true); } }
                timeline.handler = function (percent, position, duration) {
                    if (finished) return;
                    current = progress(position, duration);
                    if (current && L.Timeline && L.Timeline.update) L.Timeline.update({hash: hash, percent: percent, time: position, duration: duration});
                    if (!internal) { finish(); onBack(); } else save(true);
                };
                L.Player.listener.follow('start', started);
                L.Player.listener.follow('destroy', destroyed);
                L.PlayerVideo.listener.follow('timeupdate', updated);
                L.PlayerVideo.listener.follow('ended', ended);
                active = {finish: finish};
                var entry = {title: video.title, url: item.url, timeline: timeline};
                L.Player.play(entry);
                L.Player.playlist([entry]);
            }, onError);
        }
        function choose(video, back, failed) {
            var qualities = video.qualities || ['full_hd', 'high', 'medium'];
            L.Select.show({title: 'Качество видео', items: qualities.map(function (q) { return {title: qualityNames[q] || q, value: q}; }),
                onSelect: function (item) { L.Select.hide(); launch(video, item.value, back, failed); },
                onBack: function () { L.Select.hide(); back(); }});
        }
        function Component(object) {
            if (!object.action || object.action === 'home') { object.action = 'authors'; object.title = 'Авторы'; }
            var self = this, dead = false, requests = [], poll = null, last = null, next = null, loading = false, count = 0;
            var pageCursor = null, previous = [];
            var html = $('<div class="boosty-page"></div>'), head = $('<div class="boosty-head"></div>');
            var tabs = $('<div class="boosty-tabs"></div>'), top = $('<div class="boosty-top"></div>');
            var lastTab = null, lastGrid = null, connection;
            var binding = L.Storage.get(tokenKey, '');
            var scroll = new L.Scroll({mask: true, over: true}), grid = $('<div class="boosty-grid"></div>');
            var body = $('<div class="boosty-body"></div>');
            var note = $('<div class="boosty-note"></div>');
            html.append(head).append(note).append(scroll.render()); scroll.append(body); body.append(grid); scroll.minus(head);
            function button(title, action, extra) {
                var el = $('<div class="selector boosty-button"></div>').text(title);
                if (extra) el.addClass(extra);
                el.on('hover:enter', action).on('hover:focus', function () {
                    last = el;
                    if (extra === 'boosty-tab' || extra === 'boosty-tab-active') lastTab = el;
                    else if (extra !== 'boosty-settings') { lastGrid = el; scroll.update(el, true); }
                });
                return el;
            }
            function focus(area, target) { L.Controller.collectionSet(area); L.Controller.collectionFocus(target, area); }
            function focusTabs() { focus(tabs, lastTab || tabs.find('.boosty-tab-active').first()); }
            function move(area, direction) {
                var current = area.find('.focus').first();
                L.Controller.collectionSet(area);
                L.Controller.collectionFocus(current, area);
                if (!root.Navigator.canmove(direction)) return false;
                root.Navigator.move(direction); return true;
            }
            function horizontal(direction) {
                var area = grid.find('.focus').length ? grid : (tabs.find('.focus').length ? tabs : top);
                if (!move(area, direction) && direction === 'left') L.Controller.toggle('menu');
            }
            function api(method, path, body, yes, no, extra) {
                var pending = {cancel: null}; requests.push(pending);
                function settled(callback, value) {
                    var index = requests.indexOf(pending);
                    if (index !== -1) requests.splice(index, 1);
                    if (!dead) callback(value);
                }
                pending.cancel = call(method, path, body, function (data) { settled(yes, data); }, function (code) { settled(no || error, code); }, extra);
            }
            function restore() { if (!dead && self.activity) self.activity.toggle(); }
            function playbackError(code) {
                if (dead) return;
                if (code === 'DEVICE_REQUIRED') error(code);
                else { L.Noty.show(message(code)); restore(); }
            }
            function unpaired() {
                grid.empty(); last = lastGrid = null;
                note.text('Один раз привяжите телевизор. Вход в Boosty выполняется на компьютере.');
                grid.append(button('Привязать телевизор', function () { push('pair', 'Подключение Boosty'); }));
            }
            function error(code) {
                loading = false;
                if (self.activity) self.activity.loader(false);
                note.text(message(code));
                if (code === 'DEVICE_REQUIRED') {
                    L.Storage.set(tokenKey, ''); binding = '';
                    grid.empty(); last = lastGrid = null;
                    if (!top.find('.boosty-settings').length) top.append(connection);
                }
                grid.append(button(code === 'DEVICE_REQUIRED' ? 'Привязать телевизор' : 'Повторить', function () {
                    if (code === 'DEVICE_REQUIRED') push('pair', 'Подключение Boosty');
                    else if (object.action === 'pair') { grid.empty(); pair(); }
                    else load();
                }));
                restore();
            }
            function card(title, subtitle, image, action, author) {
                var el = $('<div class="selector boosty-card"></div>');
                if (author) el.addClass('boosty-author');
                var picture = $('<div class="boosty-picture"></div>').append($('<span></span>').text('B'));
                if (image) {
                    var img = $('<img loading="lazy" alt="">').attr('src', image);
                    img.on('error', function () { img.remove(); }); picture.append(img);
                }
                el.append(picture).append($('<div class="boosty-title"></div>').text(title));
                if (subtitle) el.append($('<div class="boosty-subtitle"></div>').text(subtitle));
                el.on('hover:enter', action).on('hover:focus', function () { last = lastGrid = el; scroll.update(el, true); });
                grid.append(el); count++;
            }
            function renderItems(data) {
                data.items.forEach(function (item) {
                    if (object.action === 'authors') {
                        card(item.title, item.description !== item.title ? item.description : '', item.image_url, function () { push('posts', item.title, item.author_id, item); }, true);
                    } else if (object.action === 'continue') {
                        card(item.title, 'Продолжить с ' + clock(item.position_seconds), item.image_url, function () { choose(item, restore, playbackError); });
                    } else {
                        item.videos.forEach(function (v) {
                            var entry = {author_id: item.author_id, post_id: item.post_id, video_id: v.video_id, title: v.title, qualities: v.qualities};
                            card(v.title, v.duration_seconds ? clock(v.duration_seconds) : item.author_id, v.image_url, function () { choose(entry, restore, playbackError); });
                        });
                    }
                });
                next = data.next_cursor;
                if (previous.length) grid.append(button('Предыдущая страница', function () { pageCursor = previous.pop(); load(); }, 'boosty-more'));
                if (next) grid.append(button('Следующая страница', function () { previous.push(pageCursor); pageCursor = next; load(); }, 'boosty-more'));
                note.text(count ? '' : (next ? 'На этой странице нет доступных видео. Можно загрузить следующую.' : 'Здесь пока нет доступных видео.'));
                loading = false; self.activity.loader(false); restore();
            }
            function load() {
                if (loading) return;
                loading = true;
                grid.empty(); note.text('Загрузка…'); count = 0; next = null; last = lastGrid = null;
                self.activity.loader(true);
                var path = '/' + object.action;
                if (object.action === 'posts') path += '?author=' + encodeURIComponent(object.author);
                if (pageCursor) path += (path.indexOf('?') === -1 ? '?' : '&') + 'cursor=' + encodeURIComponent(pageCursor);
                api('GET', path, null, renderItems);
            }
            function pair() {
                note.text('Подтвердите код в панели подключения на компьютере. Код действует 5 минут.');
                api('POST', '/pairing', {}, function (data) {
                    grid.empty().append($('<div class="boosty-code"></div>').text(data.code));
                    grid.append(button('Назад', function () { L.Activity.backward(); }));
                    restore();
                    function check() {
                        api('GET', '/pairing/' + data.id, null, function (result) {
                            if (result.token) {
                                L.Storage.set(tokenKey, result.token); connection.detach(); last = lastGrid = null;
                                note.text('Телевизор подключён'); grid.empty().append(button('Открыть Boosty', function () { push('home'); })); restore();
                            }
                            else poll = setTimeout(check, 2000);
                        }, error, {'X-Pair-Secret': data.secret});
                    }
                    poll = setTimeout(check, 2000);
                });
            }
            this.create = function () {
                top.append($('<div class="boosty-heading"></div>').text(object.title || 'Boosty'));
                connection = button('Подключение', function () { push('pair', 'Подключение Boosty'); }, 'boosty-settings');
                if (!L.Storage.get(tokenKey, '')) top.append(connection);
                [['authors', 'Авторы'], ['feed', 'Новые видео'], ['continue', 'Продолжить']].forEach(function (tab) {
                    var selected = object.action === tab[0] || (object.action === 'posts' && tab[0] === 'authors');
                    tabs.append(button(tab[1], function () { if (object.action !== tab[0]) push(tab[0], tab[1]); }, selected ? 'boosty-tab-active' : 'boosty-tab'));
                });
                head.append(top).append(tabs);
                if (object.action === 'posts' && object.profile) {
                    var profile = object.profile, cover = $('<div class="boosty-cover"></div>');
                    if (profile.cover_url) {
                        var coverImage = $('<img alt="">').attr('src', profile.cover_url);
                        coverImage.on('error', function () { coverImage.remove(); }); cover.append(coverImage);
                    }
                    var caption = $('<div class="boosty-cover-caption"></div>');
                    caption.append($('<div class="boosty-heading"></div>').text(profile.title));
                    if (profile.description && profile.description !== profile.title) caption.append($('<div></div>').text(profile.description));
                    cover.append(caption); body.prepend(cover);
                }
                if (!L.Storage.get(tokenKey, '') && object.action !== 'pair') {
                    unpaired();
                } else if (object.action === 'pair') pair();
                else load();
                return html;
            };
            this.start = function () {
                if (dead) return;
                var currentBinding = L.Storage.get(tokenKey, '');
                if (binding !== currentBinding) {
                    binding = currentBinding;
                    requests.forEach(function (request) { if (request.cancel) request.cancel(); }); requests = []; loading = false;
                    if (binding) connection.detach();
                    else if (!top.find('.boosty-settings').length) top.append(connection);
                    if (object.action !== 'pair') { if (binding) load(); else unpaired(); }
                }
                L.Controller.add('content', {
                    toggle: function () { focus(html, last || (grid.find('.selector').length ? grid.find('.selector').first() : lastTab || tabs.find('.boosty-tab-active').first())); },
                    up: function () {
                        if (grid.find('.focus').length) { if (!move(grid, 'up')) focusTabs(); }
                        else if (tabs.find('.focus').length && top.find('.selector').length) focus(top, connection);
                        else L.Controller.toggle('head');
                    },
                    down: function () {
                        if (tabs.find('.focus').length) {
                            if (grid.find('.selector').length) focus(grid, lastGrid || grid.find('.selector').first());
                        } else if (top.find('.focus').length) focusTabs();
                        else move(grid, 'down');
                    },
                    right: function () { horizontal('right'); },
                    left: function () { horizontal('left'); },
                    back: function () { L.Activity.backward(); }
                });
                L.Controller.toggle('content');
            };
            this.render = function () { return html; };
            this.pause = function () {};
            this.stop = function () {};
            this.destroy = function () { dead = true; clearTimeout(poll); requests.forEach(function (request) { if (request.cancel) request.cancel(); }); scroll.destroy(); html.remove(); };
        }
        $('<style></style>').text('.boosty-page{height:100%;padding:1.2em 1.8em;box-sizing:border-box}.boosty-head{margin-bottom:1em}.boosty-top{display:flex;align-items:center;gap:1em;margin-bottom:1em}.boosty-tabs{display:flex;gap:.6em;flex-wrap:wrap}.boosty-tabs .boosty-button{background:transparent;border-bottom:.15em solid transparent;border-radius:.2em}.boosty-tabs .boosty-tab-active{border-color:#f48756;color:#f9aa83}.boosty-settings{font-size:.85em}.boosty-card.boosty-author{background:transparent;aspect-ratio:1 / 1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:.7em}.boosty-author .boosty-picture{width:52%;height:auto;aspect-ratio:1 / 1;border-radius:50%;margin:0 auto .5em;flex-shrink:0;background:transparent}.boosty-author .boosty-title,.boosty-author .boosty-subtitle{text-shadow:0 1px 2px #000,0 0 5px #000}.boosty-author .boosty-subtitle{max-height:2.6em;overflow:hidden}.boosty-cover{position:relative;height:12em;margin:.6em .3em 1em;border-radius:.7em;overflow:hidden;background:#323842}.boosty-cover>img{width:100%;height:100%;object-fit:cover}.boosty-cover-caption{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:flex-end;padding:1.2em;background:linear-gradient(transparent,rgba(0,0,0,.85))}.boosty-cover-caption .boosty-heading{flex:none;margin-bottom:.2em}.boosty-heading{font-size:1.8em;flex:1}.boosty-note{margin:.6em 0;color:#ccc;max-width:55em}.boosty-grid{display:flex;flex-wrap:wrap;gap:1.4em;padding:.6em .3em 4em}.boosty-card{width:calc(25% - 1.4em);min-width:12em;border-radius:.7em;padding:.5em;box-sizing:border-box;background:#242830}.boosty-picture{position:relative;background:#323842;height:8em;border-radius:.4em;overflow:hidden;display:flex;align-items:center;justify-content:center}.boosty-picture span{font-size:3em;color:#ed7447}.boosty-picture img{position:absolute;width:100%;height:100%;object-fit:cover}.boosty-title{margin-top:.6em;font-size:1.05em;line-height:1.3;max-height:3.9em;overflow:hidden}.boosty-subtitle{margin-top:.4em;color:#bbb;font-size:.85em}.boosty-button{padding:.7em 1em;background:#323842;border-radius:.45em}.boosty-page .selector.focus{outline:.2em solid #f48756;background:#45404a}.boosty-page .boosty-author.focus{outline:none;background:transparent}.boosty-author.focus .boosty-picture{box-shadow:0 0 0 .2em #f48756}.boosty-author.focus .boosty-title{color:#ffb38c}.boosty-code{width:100%;font-size:4em;letter-spacing:.25em;color:#f48756}.boosty-more{width:100%;text-align:center}@media(max-width:800px){.boosty-card{width:calc(50% - 1.4em);min-width:0}.boosty-page{padding:1em}.boosty-heading{font-size:1.4em}.boosty-tabs{gap:.2em}}').appendTo('head');
        L.Component.add('boosty', Component);
        var menu = $('<li class="menu__item selector"><div class="menu__ico"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M5 3h8a5 5 0 0 1 3 9 5 5 0 0 1-3 9H5V3zm4 4v3h4a1.5 1.5 0 0 0 0-3H9zm0 7v3h4a1.5 1.5 0 0 0 0-3H9z"/></svg></div><div class="menu__text">Boosty</div></li>');
        menu.on('hover:enter', function () { push('home'); });
        $('.menu .menu__list').first().append(menu);
    }
    if (root.appready) init();
    else if (root.Lampa && root.Lampa.Listener) root.Lampa.Listener.follow('app', function (e) { if (e.type === 'ready') init(); });
})(typeof window !== 'undefined' ? window : this);
