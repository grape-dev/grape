// ====== 项目模板页 ======
// 翻译后端返回的分类名：语言包优先，无则 fallback 原名
function _tmplCatName(name) {
  if (!name) return '';
  var t = I18N.t('templates.cat.' + name);
  return t !== ('templates.cat.' + name) ? t : name;
}

let _tmplState = {
  categories: [],
  activeCat: 0,
  page: 0,
  search: '',
  featuredIdx: null,
  _favState: JSON.parse(localStorage.getItem('grape_template_favs') || '{}'),
};

function renderTemplates() {
  const d = document.getElementById('pageContent');
  if (!d) return;
  renderSelectionShell('templates', '<div class="loading">' + I18N.t('templates.loading') + '</div>');
  API.get('/project-templates').then(data => {
    const rawCats = data.categories || [];
    const allTemplates = rawCats.flatMap(cat =>
      (cat.templates || []).map(t => ({...t, _cat_name: cat.name, _cat_icon: cat.icon || '📦'}))
    );
    var _favTemplates = allTemplates.filter(function(t) { return _tmplState._favState[t.id]; });
    _tmplState.categories = [{
      id: '_fav', name: I18N.t('templates.fav_cat'), icon: '⭐',
      description: I18N.t('templates.fav_cat_desc'),
      scene_id: 'default', page_size: 9, templates: _favTemplates,
    }, {
      id: 'all', name: I18N.t('templates.all'), icon: '📋',
      description: I18N.t('templates.all_desc'),
      scene_id: 'default', page_size: 9, templates: allTemplates,
    }, ...rawCats];
    _tmplState.activeCat = 1;
    _tmplState.page = 0;
    _tmplState.search = '';
    renderTemplateCategory();
  }).catch(e => {
    if (localStorage.getItem('sidebarTab') !== 'templates') return;
    const c = document.getElementById('ptContent');
    if (c) c.innerHTML = '<div class="error">' + I18N.t('templates.load_failed', {msg: e.message}) + '</div>';
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
  renderTemplateCategory();
}

function goTemplatePage(dir) {
  var cat = _tmplState.categories[_tmplState.activeCat];
  var total = (cat.templates || []).length;
  var pages = Math.max(1, Math.ceil(total / 12));
  _tmplState.page = (_tmplState.page + dir + pages) % pages;
  renderTemplateCategory();
}

// ====== 精选模板竖排卡片 ======
function _renderFeaturedCarousel() {
  var cats = _tmplState.categories.filter(function(x) { return x.id !== 'all' && x.id !== '_fav'; });
  if (cats.length === 0) return '';

  var itemsHtml = '';

  for (var si = 0; si < cats.length; si++) {
    var x = cats[si];
    var tmplItem = null;

    if (x.templates && x.templates.length > 0) {
      tmplItem = x.templates[0];
    }

    if (tmplItem) {
      var nm = tmplItem.name || tmplItem.display_name || tmplItem.id || '';
      var ic = tmplItem.icon || '📄';
      itemsHtml += '<div class="feat-item" onclick="createFromTemplate(\'' + tmplItem.id + '\',\'' + (tmplItem.default_repo_name || tmplItem.id) + '\')">' +
        '<span class="feat-item-ic">' + ic + '</span>' +
        '<div class="feat-item-info">' +
          '<span class="feat-item-name">' + nm + '</span>' +
          '<span class="feat-item-cat">' + x.name + '</span>' +
        '</div>' +
      '</div>';
    } else {
      itemsHtml += '<div class="feat-item" onclick="onFeaturedClick(' + si + ')">' +
        '<span class="feat-item-ic">⏳</span>' +
        '<div class="feat-item-info">' +
          '<span class="feat-item-name" style="color:var(--t3);font-size:12px">' + I18N.t('templates.loading') + '</span>' +
          '<span class="feat-item-cat">' + _tmplCatName(x.name) + '</span>' +
        '</div>' +
      '</div>';
    }
  }

  return '<div class="feat-single">' +
    '<div class="feat-single-h">' + I18N.t('templates.featured_title') + '</div>' +
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

// ====== 平台来源标签 ======
function _platformTagHtml(t) {
  var p = t.source_platform || '';
  if (!p) return '';
  var label = p === 'github' ? 'GitHub' : p === 'gitcode' ? 'GitCode' : '';
  if (!label) return '';
  return '<span class="tg tg-d tmpl-platform-tag">' + label + '</span>';
}

// ====== 紧凑模板卡片 ======
function _renderHorizontalCards(templates, cat, skipPager) {
  var total = templates.length;
  var pages = Math.max(1, Math.ceil(total / 12));
  if (_tmplState.page >= pages) _tmplState.page = 0;
  var pageT = templates.slice(_tmplState.page * 12, (_tmplState.page + 1) * 12);

  var cards = pageT.map(function(t) {
    var catName = _tmplCatName(t._cat_name || cat.name || '');
    var tplUrl = t.html_url || (t.source_repo ? (t.source_platform === 'github' ? 'https://github.com/' + t.source_repo : 'https://gitcode.com/' + t.source_repo) : '');
    var cardClick = tplUrl
      ? "window.open('" + tplUrl + "','_blank')"
      : "createFromTemplate('" + t.id + "','" + (t.default_repo_name || t.id) + "')";
    var isFav = _tmplState._favState[t.id] ? true : false;
    var favStar = isFav ? '★' : '☆';
    var favCls = isFav ? 'tmpl-fav-btn tmpl-fav-act' : 'tmpl-fav-btn';
    var platformTag = _platformTagHtml(t);

    return '<div class="template-card-item" onclick="' + cardClick + '">' +
      '<span class="template-card-item-ic">' + (t.icon || '📦') + '</span>' +
      '<div class="template-card-item-info">' +
        '<div class="template-card-item-name" title="' + (t.name || '').replace(/"/g, '&quot;') + '">' +
          (t.name || t.id || '') + platformTag +
        '</div>' +
        '<div class="template-card-item-cat">' + catName + '</div>' +
        '<div class="template-card-item-desc">' + (t.description || I18N.t('templates.no_description')) + '</div>' +
        (t.recommend_reason ? '<div class="template-card-item-reason" title="' + t.recommend_reason.replace(/"/g, '&quot;') + '">💡 ' + t.recommend_reason + '</div>' : '') +
      '</div>' +
      '<span class="' + favCls + '" style="font-size:16px;cursor:pointer;flex-shrink:0;padding:0 2px;line-height:1" onclick="event.stopPropagation();toggleFav(\'' + t.id + '\')">' + favStar + '</span>' +
      '<button class="template-card-item-btn" onclick="event.stopPropagation();createFromTemplate(\'' + t.id + "','" + (t.default_repo_name || t.id) + '\')">' + I18N.t('templates.create_from_template_arrow') + '</button>' +
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
  renderTemplateCategory();
}

// ====== 主渲染 ======
function renderTemplateCategory() {
  if (localStorage.getItem('sidebarTab') !== 'templates') return;
  var cats = _tmplState.categories;
  var c = document.getElementById('ptContent');
  if (!c) return;
  if (!cats || cats.length === 0) {
    c.innerHTML = '<div class="lobby-empty" style="border:none;background:none;color:var(--t2)"><div class="lobby-empty-ic">📦</div><h3>' + I18N.t('templates.no_templates_title') + '</h3><p>' + I18N.t('templates.no_templates_desc') + '</p></div>';
    return;
  }
  var cat = _tmplState.categories[_tmplState.activeCat] || cats[0];

  if (cat.id === '_fav') {
    var _allTmpls = cats.filter(function(x) { return x.id !== '_fav' && x.id !== 'all'; }).flatMap(function(x) { return x.templates || []; });
    cat.templates = _allTmpls.filter(function(t) { return _tmplState._favState[t.id]; });
  }

  var tabs = cats.map(function(x, i) {
    var cnt = (x.templates || []).length;
    var act = i === _tmplState.activeCat ? ' tmpl-tab-act' : '';
    var favCls = x.id === '_fav' ? ' tmpl-tab-fav' : '';
    return '<button class="tmpl-tab' + act + favCls + '" onclick="switchTemplateCat(' + i + ')"><span class="tmpl-tab-ic">' + (x.icon || '📦') + '</span><span class="tmpl-tab-name">' + _tmplCatName(x.name) + '</span><span class="tmpl-tab-cnt">' + cnt + '</span></button>';
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
      '<h2>' + I18N.t('templates.gallery_title') + '</h2>' +
      '<div class="tmpl-search-bar-inline">' +
        '<input id="tmplSearchInput" type="text" placeholder="' + I18N.t('templates.search_placeholder') + '" value="' + (_tmplState.search || '') + '" onkeydown="if(event.key===\'Enter\')doSearch()">' +
        '<button onclick="doSearch()">' + I18N.t('templates.search_btn') + '</button>' +
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

function _findTemplateById(id) {
  for (var i = 0; i < _tmplState.categories.length; i++) {
    var cat = _tmplState.categories[i];
    for (var j = 0; j < (cat.templates || []).length; j++) {
      if (cat.templates[j].id === id) return cat.templates[j];
    }
  }
  return null;
}

var _PIPELINE_STAGES = {
  'default':       ['\u9700\u6c42\u5206\u6790','Issue\u8fd0\u8425','\u7f16\u7801\u5b9e\u73b0','PR\u5ba1\u67e5','\u4e91\u90e8\u7f72'],
  'hello-demo':    ['\u9700\u6c42\u5206\u6790','Issue\u5206\u7c7b','\u7f16\u7801\u5b9e\u73b0','PR\u5ba1\u67e5','\u4e91\u90e8\u7f72'],
  'cloud-native':  ['\u9700\u6c42\u5206\u6790','Issue\u8fd0\u8425','\u7f16\u7801\u5b9e\u73b0','PR\u5ba1\u67e5','\u4e91\u90e8\u7f72'],
  'iac':           ['IaC\u9700\u6c42','IaC Issue','IaC\u7f16\u7801','IaC\u5ba1\u67e5','IaC\u90e8\u7f72'],
  'agent-dev':     ['\u9700\u6c42\u5206\u6790','Issue\u8fd0\u8425','\u7f16\u7801\u5b9e\u73b0','PR\u5ba1\u67e5','\u4e91\u90e8\u7f72'],
  'skill-dev':     ['\u9700\u6c42\u5206\u6790','Skill Issue','Skill\u521b\u5efa','Skill\u6d4b\u8bd5','Skill\u53d1\u5e03'],
  'api-skill-dev': ['\u9700\u6c42\u5206\u6790','Skill Issue','API Skill\u521b\u5efa','API Skill\u6d4b\u8bd5','Skill\u53d1\u5e03'],
  'blog-publish':  ['\u535a\u5ba2\u9700\u6c42','Issue\u8fd0\u8425','\u535a\u5ba2\u751f\u6210','\u535a\u5ba2\u53d1\u5e03'],
  'harmony':       ['\u9700\u6c42\u5206\u6790','Issue\u8fd0\u8425','\u7f16\u7801\u5b9e\u73b0','PR\u5ba1\u67e5','\u4e91\u90e8\u7f72'],
};

function createFromTemplate(templateId, defaultName) {
  var t = _findTemplateById(templateId) || {};
  var tplName = t.name || templateId;
  var tplIcon = t.icon || '📦';
  var tplDesc = t.description || '';
  var tplTags = t.tags || [];
  var tplScene = t.scene_id || 'default';
  var tplPlatform = t.source_platform || '';
  var tplSource = t.source || '';
  var tplStats = t.stats || {};
  var tplReason = t.recommend_reason || '';
  var tplUrl = t.html_url || '';

  var platformTag = tplPlatform === 'github' ? '<span class="tg tg-d tmpl-platform-tag" style="margin-left:6px">GitHub</span>'
    : tplPlatform === 'gitcode' ? '<span class="tg tg-d tmpl-platform-tag" style="margin-left:6px">GitCode</span>' : '';

  var sourceBadge = tplSource === 'local'
    ? '<span style="font-size:10px;color:var(--g);background:#e8f5e9;padding:2px 8px;border-radius:4px">\u672c\u5730\u6a21\u677f</span>'
    : tplSource === 'git'
    ? '<span style="font-size:10px;color:var(--sk);background:#e3f2fd;padding:2px 8px;border-radius:4px">\u4e91\u7aef\u9879\u76ee</span>'
    : '';

  var starsHtml = '';
  if (tplStats.show && tplStats.stars) {
    starsHtml = '<span style="font-size:11px;color:var(--y)">\u2b50 ' + (tplStats.stars >= 1000 ? (tplStats.stars / 1000).toFixed(1) + 'k' : tplStats.stars) + '</span>';
  }

  var langHtml = '';
  if (tplStats.language) {
    langHtml = '<span style="font-size:11px;color:var(--t3)">' + tplStats.language + '</span>';
  }

  var tagsHtml = tplTags.length
    ? '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:5px">' + tplTags.map(function(tg) {
        return '<span class="tg tg-d" style="font-size:10px">#' + tg + '</span>';
      }).join('') + '</div>'
    : '';

  var reasonHtml = tplReason
    ? '<div style="margin-top:6px;font-size:11px;color:var(--a);background:#f0f4f8;padding:6px 10px;border-radius:6px">\ud83d\udca1 ' + tplReason + '</div>'
    : '';

  var stages = _PIPELINE_STAGES[tplScene] || _PIPELINE_STAGES['default'];
  var stagesHtml = stages.map(function(s, i) {
    var num = i + 1;
    var isLast = i === stages.length - 1;
    var arrow = isLast ? '' : '<span style="color:var(--t3);font-size:10px;margin:0 3px">\u2192</span>';
    return '<span style="display:inline-flex;align-items:center"><span style="width:16px;height:16px;border-radius:50%;background:var(--a);color:#fff;font-size:9px;display:flex;align-items:center;justify-content:center;flex-shrink:0">' + num + '</span><span style="font-size:11px;color:var(--t2);margin-left:3px">' + s + '</span></span>' + arrow;
  }).join('');

  var steps = [
    '\u521b\u5efa GitCode \u4ed3\u5e93\uff08\u4f60\u7684\u4e2a\u4eba\u540d\u4e0b\uff09',
    '\u63a8\u9001\u6a21\u677f\u4ee3\u7801\u5230\u4ed3\u5e93',
    '\u914d\u7f6e\u4ed3\u5e93\u6258\u7ba1\u4e0e Webhook \u81ea\u52a8\u5316',
    '\u5e94\u7528 ' + tplScene + ' \u5f00\u53d1\u573a\u666f\uff08' + stages.length + ' \u9636\u6bb5\u6d41\u6c34\u7ebf\uff09',
    '\u521b\u5efa\u5f15\u5bfc Issue\uff0c\u81ea\u52a8\u89e6\u53d1\u6d41\u6c34\u7ebf',
  ];
  var stepsHtml = steps.map(function(s) {
    return '<div style="display:flex;align-items:flex-start;gap:6px;margin-bottom:5px">' +
      '<span style="color:var(--g);font-size:11px;flex-shrink:0;margin-top:1px">\u2713</span>' +
      '<span style="font-size:11px;color:var(--t2);line-height:1.5">' + s + '</span>' +
    '</div>';
  }).join('');

  var externalLink = tplUrl
    ? '<a href="' + tplUrl + '" target="_blank" style="font-size:10px;color:var(--a);text-decoration:none;margin-left:8px">\u67e5\u770b\u539f\u9879\u76ee \u2197</a>'
    : '';

  var modal = document.createElement('div');
  modal.id = 'ptCreateModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML =
    '<div class="cd" style="width:520px;padding:24px;border-radius:12px;max-height:90vh;overflow-y:auto">' +
      '<div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:14px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="display:flex;align-items:center;gap:10px">' +
            '<span style="font-size:28px;flex-shrink:0">' + tplIcon + '</span>' +
            '<div style="flex:1;min-width:0">' +
              '<div style="display:flex;align-items:center;flex-wrap:wrap">' +
                '<span style="font-size:16px;font-weight:700;color:var(--t)">' + tplName + '</span>' +
                platformTag +
              '</div>' +
              '<div style="display:flex;align-items:center;gap:8px;margin-top:3px">' +
                sourceBadge + starsHtml + langHtml + externalLink +
              '</div>' +
            '</div>' +
          '</div>' +
          '<p style="font-size:12px;color:var(--t2);line-height:1.6;margin-top:8px">' + tplDesc + '</p>' +
          tagsHtml +
          reasonHtml +
        '</div>' +
        '<span style="font-size:18px;color:var(--t3);cursor:pointer;flex-shrink:0;margin:-4px -4px 0 0;padding:4px" onclick="document.getElementById(\'ptCreateModal\').remove()">\u00d7</span>' +
      '</div>' +

      '<div style="height:1px;background:var(--b);margin:14px 0"></div>' +

      '<div style="margin-bottom:14px">' +
        '<div style="font-size:12px;font-weight:600;color:var(--t);margin-bottom:8px">\ud83d\ude80 \u521b\u5efa\u540e\u5c06\u81ea\u52a8\u5b8c\u6210</div>' +
        stepsHtml +
      '</div>' +

      '<div style="margin-bottom:16px">' +
        '<div style="font-size:12px;font-weight:600;color:var(--t);margin-bottom:8px">\ud83d\udca1 \u6d41\u6c34\u7ebf\u9884\u89c8\uff08' + tplScene + '\uff09</div>' +
        '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:0;background:var(--bg);padding:10px 12px;border-radius:8px">' +
          stagesHtml +
        '</div>' +
      '</div>' +

      '<div style="height:1px;background:var(--b);margin:0 0 14px"></div>' +

      '<label class="ts t2" style="display:block;margin-bottom:4px;font-size:12px">' + I18N.t('templates.repo_name') + '</label>' +
      '<input id="ptRepoName" value="' + defaultName + '" style="width:100%;padding:9px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:13px;outline:none;background:var(--c);box-sizing:border-box">' +

      '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">' +
        '<button class="bt bt-o bt-s" onclick="document.getElementById(\'ptCreateModal\').remove()">' + I18N.t('templates.cancel') + '</button>' +
        '<button class="bt bt-p bt-s" id="ptCreateBtn" onclick="doCreateFromTemplate(\'' + templateId + '\')" style="padding:6px 18px">' + I18N.t('templates.create_from_template_arrow') + '</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(modal);
  document.getElementById('ptRepoName').focus();
  document.getElementById('ptRepoName').select();
}

function doCreateFromTemplate(templateId) {
  var repoName = (document.getElementById('ptRepoName').value || '').trim();
  var btn = document.getElementById('ptCreateBtn');
  btn.disabled = true;
  btn.textContent = I18N.t('templates.creating');

  // 进度条 UI
  var progressEl = document.getElementById('ptProgress');
  if (!progressEl) {
    progressEl = document.createElement('div');
    progressEl.id = 'ptProgress';
    progressEl.style.cssText = 'margin-top:14px';
    var modalBody = btn.closest('.cd');
    if (modalBody) modalBody.appendChild(progressEl);
  }
  var _step = 0;
  var _steps = [
    {pct: 10, text: I18N.t('templates.progress_checking')},
    {pct: 25, text: I18N.t('templates.progress_creating_repo')},
    {pct: 45, text: I18N.t('templates.progress_downloading')},
    {pct: 65, text: I18N.t('templates.progress_pushing')},
    {pct: 80, text: I18N.t('templates.progress_hosting')},
    {pct: 95, text: I18N.t('templates.progress_applying_scene')},
  ];
  function setProgress(pct, text) {
    var bar = pct + '%';
    progressEl.innerHTML =
      '<div style="font-size:11px;color:var(--t2);margin-bottom:6px">' + text + '</div>' +
      '<div style="height:6px;background:var(--bg);border-radius:3px;overflow:hidden">' +
        '<div style="height:100%;width:' + bar + ';background:var(--a);border-radius:3px;transition:width .4s ease"></div>' +
      '</div>' +
      '<div style="text-align:right;font-size:10px;color:var(--t3);margin-top:3px">' + pct + '%</div>';
  }
  function nextStep() {
    if (_step < _steps.length) {
      setProgress(_steps[_step].pct, _steps[_step].text);
      _step++;
    }
  }
  // 模拟进度推进（实际 API 是同步的，用定时器推进）
  var _timer = setInterval(nextStep, 5000);
  // 第一步立即显示
  setProgress(10, I18N.t('templates.progress_checking'));

  API.get('/repos/check-name?name=' + encodeURIComponent(repoName))
    .then(function(check) {
      if (check.exists) {
        clearInterval(_timer);
        btn.disabled = false;
        btn.textContent = I18N.t('templates.create_btn');
        progressEl.innerHTML = '';
        app.showToast(I18N.t('templates.repo_exists', {full_name: check.full_name}));
        var input = document.getElementById('ptRepoName');
        if (input) { input.focus(); input.select(); }
        return;
      }
      setProgress(25, '正在创建 GitCode 仓库...');
      API.post('/project-templates/create', {template_id: templateId, repo_name: repoName}, 300000).then(function(r) {
        clearInterval(_timer);
        setProgress(100, I18N.t('templates.progress_done'));
        var modal = document.getElementById('ptCreateModal');
        if (modal) setTimeout(function() { modal.remove(); }, 800);
        if (r.host_warning) {
          app.showToast(r.host_warning, 10000);
        } else {
          app.showToast(I18N.t('templates.create_success', {msg: r.message || I18N.t('templates.create_success_default')}));
        }
        var parts = (r.full_name || '').split('/');
        setTimeout(function() { selectRepo(r.full_name, parts[0], parts[1]); }, 900);
      }).catch(function(e) {
        clearInterval(_timer);
        btn.disabled = false;
        btn.textContent = I18N.t('templates.create_btn');
        setProgress(0, I18N.t('templates.progress_failed', {msg: e.message}));
        app.showToast(I18N.t('templates.create_failed', {msg: e.message}));
      });
    })
    .catch(function(err) {
      clearInterval(_timer);
      btn.disabled = false;
      btn.textContent = I18N.t('templates.create_btn');
      progressEl.innerHTML = '';
      app.showToast(I18N.t('templates.check_failed', {msg: err.message || err}));
    });
}
