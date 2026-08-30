// ====== 项目模板页 ======
let _tmplState = {
  categories: [],
  activeCat: 0,
  page: 0,
  search: '',
  featuredIdx: null,
  top1000Cache: {},
  _favState: JSON.parse(localStorage.getItem('grape_template_favs') || '{}'),
};

function renderTemplates() {
  const d = document.getElementById('pageContent');
  if (!d) return;
  renderSelectionShell('templates', '<div class="loading">加载中...</div>');
  API.get('/project-templates').then(data => {
    const rawCats = data.categories || [];
    const nonTop1000Cats = rawCats.filter(cat => !cat.top1000);
    const allTemplates = nonTop1000Cats.flatMap(cat =>
      (cat.templates || []).map(t => ({...t, _cat_name: cat.name, _cat_icon: cat.icon || '📦'}))
    );
    var _favTemplates = allTemplates.filter(function(t) { return _tmplState._favState[t.id]; });
    _tmplState.categories = [{
      id: '_fav', name: '收藏', icon: '⭐',
      description: '你收藏的项目模板。',
      scene_id: 'default', page_size: 9, templates: _favTemplates,
    }, {
      id: 'all', name: '全部', icon: '📋',
      description: '浏览所有可用项目模板，一键创建你的 DevOps 项目',
      scene_id: 'default', page_size: 9, templates: allTemplates,
    }, ...rawCats];
    _tmplState.activeCat = 1;
    _tmplState.page = 0;
    _tmplState.search = '';
    _tmplState.top1000Cache = {};
    rawCats.forEach(function(c) {
      if (c.top1000 || (c.name || '').indexOf('GitHub') >= 0 || (c.name || '').indexOf('GitCode') >= 0) {
        _tmplState.top1000Cache[c.id] = {
          items: c.templates || [], total: (c.templates || []).length,
          page: 0, loading: false, error: null,
          search: '', sortBy: 'stars', totalPages: 1, loaded: true,
        };
      }
    });
    renderTemplateCategory();
    rawCats.forEach(function(c) {
      if (_tmplState.top1000Cache[c.id]) {
        var idx = _tmplState.categories.findIndex(function(x) { return x.id === c.id; });
        if (idx >= 0) loadTop1000(idx);
      }
    });
  }).catch(e => {
    const c = document.getElementById('ptContent');
    if (c) c.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function switchTemplateCat(idx) {
  _tmplState.activeCat = idx;
  _tmplState.page = 0;
  var cat = _tmplState.categories[idx];
  if (cat.id === '_fav' || cat.id === 'all') {
    _tmplState.featuredIdx = null;
  } else {
    _tmplState.featuredIdx = idx - 2;
  }
  if (cat && (cat.top1000 || (cat.name || '').indexOf('GitHub') >= 0 || (cat.name || '').indexOf('GitCode') >= 0)) {
    var cache = _tmplState.top1000Cache[cat.id];
    if (!cache || !cache.loaded || cache.search !== _tmplState.search) {
      loadTop1000(idx);
    } else {
      renderTemplateCategory();
    }
    return;
  }
  renderTemplateCategory();
}

function _tmplPageSize(cat) {
  return 12;
}

function goTemplatePage(dir) {
  var cat = _tmplState.categories[_tmplState.activeCat];
  var total = (cat.templates || []).length;
  var pages = Math.max(1, Math.ceil(total / 12));
  _tmplState.page = (_tmplState.page + dir + pages) % pages;
  renderTemplateCategory();
}

function _sceneClass(sceneId) {
  var s = (sceneId || '').toLowerCase();
  if (s.includes('web') || s.includes('app')) return 'sc-web';
  if (s.includes('data') || s.includes('ml') || s.includes('etl')) return 'sc-data';
  if (s.includes('infra') || s.includes('devops') || s.includes('k8s')) return 'sc-infra';
  return 'sc-default';
}

// ====== 精选模板竖排卡片 ======
const _MAX_FEATURED = 5;

function _renderFeaturedCarousel() {
  var cats = _tmplState.categories.filter(function(x) { return x.id !== 'all' && x.id !== '_fav'; });
  if (cats.length === 0) return '';

  // 新规则：按「创建过该模板的不同用户数 create_count」降序取前 N
  var ranked = [];
  cats.forEach(function(cat) {
    (cat.templates || []).forEach(function(t) {
      var cnt = t.create_count || 0;
      if (cnt > 0) ranked.push({cnt: cnt, cat: cat.name, t: t});
    });
  });
  ranked.sort(function(a, b) {
    if (b.cnt !== a.cnt) return b.cnt - a.cnt;
    return (a.t.name || '').localeCompare(b.t.name || '');
  });

  var itemsHtml = '';
  if (ranked.length > 0) {
    ranked.slice(0, _MAX_FEATURED).forEach(function(it) {
      var t = it.t;
      var nm = t.name || t.display_name || t.id || '';
      var ic = t.icon || '📄';
      itemsHtml += '<div class="feat-item" onclick="createFromTemplate(\'' + t.id + '\',\'' + (t.default_repo_name || t.id) + '\')">' +
        '<span class="feat-item-ic">' + ic + '</span>' +
        '<div class="feat-item-info">' +
          '<span class="feat-item-name">' + nm + '</span>' +
          '<span class="feat-item-cat">' + it.cat + '</span>' +
        '</div>' +
      '</div>';
    });
    return '<div class="feat-single">' +
      '<div class="feat-single-h">精选推荐</div>' +
      '<div class="feat-single-body">' + itemsHtml + '</div>' +
    '</div>';
  }

  // 回退：暂无使用数据 → 每分类取第一个（不空白）
  for (var si = 0; si < cats.length; si++) {
    var x = cats[si];
    var tmplItem = null;

    // 第一步：查 top1000Cache（最新异步数据）
    var cache = _tmplState.top1000Cache[x.id];
    if (cache && cache.items && cache.items.length > 0) {
      tmplItem = cache.items[0];
    }
    // 第二步：回退到 cat.templates（初始化时 API 就已包含的数据）
    if (!tmplItem && x.templates && x.templates.length > 0) {
      tmplItem = x.templates[0];
    }

    if (tmplItem) {
      var nm2 = tmplItem.name || tmplItem.display_name || tmplItem.id || '';
      var ic2 = tmplItem.icon || '📄';
      itemsHtml += '<div class="feat-item" onclick="createFromTemplate(\'' + tmplItem.id + '\',\'' + (tmplItem.default_repo_name || tmplItem.id) + '\')">' +
        '<span class="feat-item-ic">' + ic2 + '</span>' +
        '<div class="feat-item-info">' +
          '<span class="feat-item-name">' + nm2 + '</span>' +
          '<span class="feat-item-cat">' + x.name + '</span>' +
        '</div>' +
      '</div>';
    } else {
      itemsHtml += '<div class="feat-item" onclick="onFeaturedClick(' + si + ')">' +
        '<span class="feat-item-ic">⏳</span>' +
        '<div class="feat-item-info">' +
          '<span class="feat-item-name" style="color:var(--t3);font-size:12px">加载中...</span>' +
          '<span class="feat-item-cat">' + x.name + '</span>' +
        '</div>' +
      '</div>';
    }
  }

  return '<div class="feat-single">' +
    '<div class="feat-single-h">精选推荐</div>' +
    '<div class="feat-single-body">' + itemsHtml + '</div>' +
  '</div>';
}

function onFeaturedClick(idx) {
  if (_tmplState.featuredIdx === idx) {
    _tmplState.featuredIdx = null;
    switchTemplateCat(0);
  } else {
    _tmplState.featuredIdx = idx;
    switchTemplateCat(idx + 2);
  }
}

// ====== 紧凑模板卡片 ======
function _renderHorizontalCards(templates, cat, skipPager) {
  var total = templates.length;
  var pages = Math.max(1, Math.ceil(total / 12));
  if (_tmplState.page >= pages) _tmplState.page = 0;
  var pageT = templates.slice(_tmplState.page * 12, (_tmplState.page + 1) * 12);

  var cards = pageT.map(function(t) {
    var catName = t._cat_name || cat.name || '';
    var tplUrl = t.source_repo ? 'https://gitcode.com/' + t.source_repo : '';
    var cardClick = tplUrl
      ? "window.open('" + tplUrl + "','_blank')"
      : "createFromTemplate('" + t.id + "','" + (t.default_repo_name || t.id) + "')";
    var isFav = _tmplState._favState[t.id] ? true : false;
    var favStar = isFav ? '★' : '☆';
    var favCls = isFav ? 'tmpl-fav-btn tmpl-fav-act' : 'tmpl-fav-btn';

    return '<div class="template-card-item" onclick="' + cardClick + '">' +
      '<span class="template-card-item-ic">' + (t.icon || '📦') + '</span>' +
      '<div class="template-card-item-info">' +
        '<div class="template-card-item-name" title="' + (t.name || '').replace(/"/g, '&quot;') + '">' + (t.name || t.id || '') + '</div>' +
        '<div class="template-card-item-cat">' + catName + '</div>' +
        '<div class="template-card-item-desc">' + (t.description || '暂无描述') + '</div>' +
      '</div>' +
      '<span class="' + favCls + '" style="font-size:16px;cursor:pointer;flex-shrink:0;padding:0 2px;line-height:1" onclick="event.stopPropagation();toggleFav(\'' + t.id + '\')">' + favStar + '</span>' +
      '<button class="template-card-item-btn" onclick="event.stopPropagation();createFromTemplate(\'' + t.id + "','" + (t.default_repo_name || t.id) + '\')">一键创建→</button>' +
    '</div>';
  }).join('');

  var pagerHtml = '';
  if (!skipPager && pages > 1) {
    pagerHtml = '<div class="top1000-pager" style="grid-column:1/-1">' +
      '<button' + (_tmplState.page === 0 ? ' disabled' : '') + ' onclick="goTemplatePage(-1)">‹</button>' +
      '<span>' + (_tmplState.page + 1) + ' / ' + pages + '</span>' +
      '<button' + (_tmplState.page >= pages - 1 ? ' disabled' : '') + ' onclick="goTemplatePage(1)">›</button>' +
    '</div>';
  }

  var grid = '<div class="template-list">' + cards + pagerHtml + '</div>';
  return grid;
}

// ====== 收藏管理 ======
function _saveFavs() {
  localStorage.setItem('grape_template_favs', JSON.stringify(_tmplState._favState));
}

function toggleFav(templateId) {
  if (_tmplState._favState[templateId]) {
    delete _tmplState._favState[templateId];
  } else {
    _tmplState._favState[templateId] = 1;
  }
  _saveFavs();
  renderTemplateCategory();
}

function _renderFavBtn(templateId) {
  var isFav = _tmplState._favState[templateId] ? true : false;
  var cls = isFav ? 'tmpl-fav-btn tmpl-fav-act' : 'tmpl-fav-btn';
  var star = isFav ? '★' : '☆';
  return '<span class="' + cls + '" onclick="event.stopPropagation();toggleFav(\'' + templateId + '\')">' + star + '</span>';
}

// ====== 搜索 ======
function doSearch() {
  var input = document.getElementById('tmplSearchInput');
  var kw = (input && input.value || '').trim();
  _tmplState.search = kw;
  _tmplState.page = 0;
  var cat = _tmplState.categories[_tmplState.activeCat];
  if (cat && (cat.top1000 || (cat.name || '').indexOf('GitHub') >= 0 || (cat.name || '').indexOf('GitCode') >= 0)) {
    var cache = _tmplState.top1000Cache[cat.id];
    if (!cache) return;
    cache.search = kw;
    cache.page = 0;
    cache.loaded = false;
    _tmplState.top1000Cache[cat.id] = cache;
    loadTop1000(_tmplState.activeCat);
  } else {
    renderTemplateCategory();
  }
}

// ====== 主渲染 ======
function renderTemplateCategory() {
  var cats = _tmplState.categories;
  var c = document.getElementById('ptContent');
  if (!c) return;
  if (!cats || cats.length === 0) {
    c.innerHTML = '<div class="lobby-empty" style="border:none;background:none;color:var(--t2)"><div class="lobby-empty-ic">📦</div><h3>暂无项目模板</h3><p>等待管理员配置项目模板。</p></div>';
    return;
  }
  var cat = _tmplState.categories[_tmplState.activeCat] || cats[0];

  if (cat.id === '_fav') {
    var _allTmpls = cats.filter(function(x) { return x.id !== '_fav' && x.id !== 'all'; }).flatMap(function(x) { return x.templates || []; });
    // 补充 top1000 缓存中的收藏项
    Object.keys(_tmplState.top1000Cache).forEach(function(catId) {
      var cache = _tmplState.top1000Cache[catId];
      if (cache && cache.items) {
        var parentCat = cats.find(function(c) { return c.id === catId; });
        cache.items.forEach(function(t) {
          _allTmpls.push({...t, _cat_name: (parentCat || {}).name || '', _cat_icon: (parentCat || {}).icon || '📦'});
        });
      }
    });
    cat.templates = _allTmpls.filter(function(t) { return _tmplState._favState[t.id]; });
  }

  if (cat.top1000 || (cat.name || '').indexOf('GitHub') >= 0 || (cat.name || '').indexOf('GitCode') >= 0) {
    renderTop1000Tab(c, cat);
    return;
  }

  var realCats = cats.filter(function(x) { return x.id !== 'all'; });
  var tabs = cats.map(function(x, i) {
    var cnt = (x.templates || []).length;
    var act = i === _tmplState.activeCat ? ' tmpl-tab-act' : '';
    var favCls = x.id === '_fav' ? ' tmpl-tab-fav' : '';
    return '<button class="tmpl-tab' + act + favCls + '" onclick="switchTemplateCat(' + i + ')"><span class="tmpl-tab-ic">' + (x.icon || '📦') + '</span><span class="tmpl-tab-name">' + x.name + '</span><span class="tmpl-tab-cnt">' + cnt + '</span></button>';
  }).join('');

  var allT = cat.templates || [];
  var filteredT = _tmplState.search
    ? allT.filter(function(t) {
        var kw = _tmplState.search.toLowerCase();
        return (t.name || '').toLowerCase().includes(kw) || (t.description || '').toLowerCase().includes(kw);
      })
    : allT;

  var featuredHtml = _renderFeaturedCarousel();
  var cardsHtml = _renderHorizontalCards(filteredT, cat, false);

  c.innerHTML = '<div class="project-page">' +
    '<div class="tmpl-search-wide">' +
      '<h2>模板库</h2>' +
      '<div class="tmpl-search-bar-inline">' +
        '<input id="tmplSearchInput" type="text" placeholder="🔍 搜索项目名称、描述..." value="' + (_tmplState.search || '') + '" onkeydown="if(event.key===\'Enter\')doSearch()">' +
        '<button onclick="doSearch()">搜索</button>' +
      '</div>' +
    '</div>' +
    '<div class="template-layout">' +
      '<div class="template-layout-main">' +
        '<div class="tmpl-tabs">' + tabs + '</div>' +
        cardsHtml +
      '</div>' +
      '<div class="template-layout-side">' +
        featuredHtml +
      '</div>' +
    '</div>' +
  '</div>';
}

// ====== Top1000 数据加载 ======
function loadTop1000(catIdx) {
  var cat = _tmplState.categories[catIdx];
  if (!cat) return;
  var prev = _tmplState.top1000Cache[cat.id] || {};
  var cache = {
    items: prev.items || [],
    total: prev.total || 0,
    page: prev.page || 0,
    loading: true,
    error: null,
    search: _tmplState.search,
    sortBy: 'stars',
    totalPages: 1,
  };
  _tmplState.top1000Cache[cat.id] = cache;
  renderTemplateCategory();

  var params = new URLSearchParams({
    platform: cat.platform,
    page: cache.page,
    size: 20,
    sort_by: cache.sortBy,
  });
  if (cache.search) params.set('keyword', cache.search);

  console.log('[top1000] loading:', cat.id, 'platform:', cat.platform, 'params:', params.toString());

  API.get('/project-templates/top1000?' + params.toString())
    .then(function(data) {
      var c2 = _tmplState.top1000Cache[cat.id];
      if (!c2) return;
      c2.items = data.templates || [];
      c2.total = data.total || 0;
      c2.loading = false;
      c2.loaded = true;
      c2.totalPages = Math.max(1, Math.ceil(c2.total / 20));
      _tmplState.top1000Cache[cat.id] = c2;
      renderTemplateCategory();
    })
    .catch(function(err) {
      var c2 = _tmplState.top1000Cache[cat.id];
      if (!c2) return;
      c2.loading = false;
      c2.error = err.message || '加载失败';
      _tmplState.top1000Cache[cat.id] = c2;
      renderTemplateCategory();
    });
}

function sortTop1000(col) {
  var cat = _tmplState.categories[_tmplState.activeCat];
  if (!cat) return;
  var cache = _tmplState.top1000Cache[cat.id] || {};
  cache.sortBy = col;
  cache.page = 0;
  cache.loaded = false;
  _tmplState.top1000Cache[cat.id] = cache;
  loadTop1000(_tmplState.activeCat);
}

function goTop1000Page(dir) {
  var cat = _tmplState.categories[_tmplState.activeCat];
  if (!cat) return;
  var cache = _tmplState.top1000Cache[cat.id];
  if (!cache) return;
  var newPage = cache.page + dir;
  if (newPage < 0 || newPage >= cache.totalPages) return;
  cache.page = newPage;
  cache.loaded = false;
  _tmplState.top1000Cache[cat.id] = cache;
  loadTop1000(_tmplState.activeCat);
}

// ====== Top1000 渲染 ======
function renderTop1000Tab(c, cat) {
  var cats = _tmplState.categories;
  var tabs = cats.map(function(x, i) {
    var cnt = x.id === '_fav' ? Object.keys(_tmplState._favState).length : (x.top1000 ? (x.total || 0) : (x.templates || []).length);
    var act = i === _tmplState.activeCat ? ' tmpl-tab-act' : '';
    var favCls = x.id === '_fav' ? ' tmpl-tab-fav' : '';
    return '<button class="tmpl-tab' + act + favCls + '" onclick="switchTemplateCat(' + i + ')"><span class="tmpl-tab-ic">' + (x.icon || '📦') + '</span><span class="tmpl-tab-name">' + x.name + '</span><span class="tmpl-tab-cnt">' + cnt + '</span></button>';
  }).join('');

  var cache = _tmplState.top1000Cache[cat.id] || {
    items: [], total: 0, page: 0, loading: false, error: null,
    search: '', sortBy: 'stars', totalPages: 1,
  };

  var featuredHtml = _renderFeaturedCarousel();
  var contentHtml = '';

  if (cache.loading) {
    contentHtml = '<div class="loading" style="padding:45px;text-align:center">⏳ 加载 Top1000 数据...</div>';
  } else if (cache.error) {
    contentHtml = '<div class="error" style="padding:40px;text-align:center">❌ ' + cache.error + '</div>';
  } else {
    var items = cache.items || [];
    var top1000Tmpls = items.map(function(t) {
      return {
        id: t.id,
        name: t.name || t.display_name || '',
        description: t.description || '',
        icon: t.icon || '#' + (t.rank || ''),
        tags: t.tags || [],
        source_repo: t.source_repo || '',
        default_repo_name: t.default_repo_name || t.id,
        _cat_name: cat.name,
      };
    });
    contentHtml = _renderHorizontalCards(top1000Tmpls, cat, true);

    var pagerHtml = '';
    if (cache.totalPages > 1) {
      var prevDisabled = cache.page <= 0 ? ' disabled' : '';
      var nextDisabled = cache.page >= cache.totalPages - 1 ? ' disabled' : '';
      pagerHtml = '<div class="top1000-pager">' +
        '<button' + prevDisabled + ' onclick="goTop1000Page(-1)">‹</button>' +
        '<span>' + (cache.page + 1) + ' / ' + cache.totalPages + ' (' + cache.total + ' 个)</span>' +
        '<button' + nextDisabled + ' onclick="goTop1000Page(1)">›</button>' +
      '</div>';
    }
    contentHtml += pagerHtml;
  }

  c.innerHTML = '<div class="project-page">' +
    '<div class="tmpl-search-wide">' +
      '<h2>模板库</h2>' +
      '<div class="tmpl-search-bar-inline">' +
        '<input id="tmplSearchInput" type="text" placeholder="🔍 搜索项目名称、描述..." value="' + (cache.search || _tmplState.search || '') + '" onkeydown="if(event.key===\'Enter\')doSearch()">' +
        '<button onclick="doSearch()">搜索</button>' +
      '</div>' +
    '</div>' +
    '<div class="template-layout">' +
      '<div class="template-layout-main">' +
        '<div class="tmpl-tabs">' + tabs + '</div>' +
        contentHtml +
      '</div>' +
      '<div class="template-layout-side">' +
        featuredHtml +
      '</div>' +
    '</div>' +
  '</div>';
}

function createFromTemplate(templateId, defaultName) {
  var modal = document.createElement('div');
  modal.id = 'ptCreateModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = '<div class="cd" style="width:440px;padding:20px;border-radius:10px">' +
    '<h3 style="font-size:14px;font-weight:600;margin-bottom:6px">一键创建项目</h3>' +
    '<p class="ts t2" style="margin-bottom:14px">将在你的 GitCode 个人账号下创建同名仓库，并自动加入项目托管、应用对应的开发场景。</p>' +
    '<label class="ts t2" style="display:block;margin-bottom:4px">仓库名</label>' +
    '<input id="ptRepoName" value="'+ defaultName + '" style="width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c)">' +
    '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">' +
      '<button class="bt bt-o bt-s" onclick="document.getElementById(\'ptCreateModal\').remove()">取消</button>' +
      '<button class="bt bt-p bt-s" id="ptCreateBtn" onclick="doCreateFromTemplate(\'' + templateId + '\')">创建</button>' +
    '</div>' +
  '</div>';
  document.body.appendChild(modal);
  document.getElementById('ptRepoName').focus();
}

function doCreateFromTemplate(templateId) {
  var repoName = (document.getElementById('ptRepoName').value || '').trim();
  var btn = document.getElementById('ptCreateBtn');
  btn.disabled = true;
  btn.textContent = '创建中...';
  API.get('/repos/check-name?name=' + encodeURIComponent(repoName))
    .then(function(check) {
      if (check.exists) {
        btn.disabled = false;
        btn.textContent = '创建';
        app.showToast('⚠️ 仓库 ' + check.full_name + ' 已存在，请更换仓库名');
        var input = document.getElementById('ptRepoName');
        if (input) { input.focus(); input.select(); }
        return;
      }
      API.post('/project-templates/create', {template_id: templateId, repo_name: repoName}).then(function(r) {
        var modal = document.getElementById('ptCreateModal');
        if (modal) modal.remove();
        app.showToast('✅ ' + (r.message || '创建成功'));
        var parts = (r.full_name || '').split('/');
        selectRepo(r.full_name, parts[0], parts[1]);
      }).catch(function(e) {
        btn.disabled = false;
        btn.textContent = '创建';
        app.showToast('创建失败: ' + e.message);
      });
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = '创建';
      app.showToast('检查失败: ' + (err.message || err));
    });
}