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
    var cardClick = "createFromTemplate('" + t.id + "','" + (t.default_repo_name || t.id) + "')";
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

var _TAG_HIGHLIGHTS = {
  'python':     'Python \u7f16\u7a0b\u5b9e\u8df5',
  'flask':      'Flask Web \u6846\u67b6',
  'fastapi':    'FastAPI \u73b0\u4ee3 API',
  'web':        'Web \u5e94\u7528\u5f00\u53d1',
  'api':        'RESTful API \u8bbe\u8ba1',
  'cli':        '\u547d\u4ee4\u884c\u5de5\u5177\u5f00\u53d1',
  'game':       '\u6e38\u620f\u903b\u8f91\u4e0e\u6e32\u67d3',
  'pygame':     'Pygame \u6e38\u620f\u5f15\u64ce',
  'starter':    '\u9002\u5408\u65b0\u624b\u5feb\u901f\u5165\u95e8',
  'test':       '\u5355\u5143\u6d4b\u8bd5\u5b9e\u8df5',
  'html':       '\u524d\u7aef\u9875\u9762\u5f00\u53d1',
  'css':        'CSS \u6837\u5f0f\u8bbe\u8ba1',
  'js':         'JavaScript \u4ea4\u4e92',
  'frontend':   '\u524d\u7aef\u4e09\u4ef6\u5957',
  'blog':       '\u535a\u5ba2\u7cfb\u7edf\u642d\u5efa',
  'docker':     '\u5bb9\u5668\u5316\u90e8\u7f72',
  'k8s':        'Kubernetes \u7f16\u6392',
  'terraform':  '\u57fa\u7840\u8bbe\u65bd\u5373\u4ee3\u7801',
  'ai':         'AI \u80fd\u529b\u96c6\u6210',
  'agent':      'Agent \u81ea\u52a8\u5316',
  'automation': '\u81ea\u52a8\u5316\u5de5\u4f5c\u6d41',
  'skill':      '\u534e\u4e3a\u4e91 Skill \u5f00\u53d1',
  'harmonyos':  '\u9e3f\u8499\u5e94\u7528\u5f00\u53d1',
  'arkts':      'ArkTS \u8bed\u8a00',
  'ecommerce':  '\u7535\u5546\u5e94\u7528',
  'mcp':        'MCP \u534f\u8bae\u96c6\u6210',
  'vue':        'Vue.js \u6846\u67b6',
  'golang':     'Go \u8bed\u8a00\u5f00\u53d1',
  'gateway':    'API \u7f51\u5173',
  'iac':        'IaC \u57fa\u7840\u8bbe\u65bd',
  'spider':     '\u722c\u866b\u4e0e\u6570\u636e\u91c7\u96c6',
  'crawler':    '\u6570\u636e\u6293\u53d6',
  'markdown':   'Markdown \u5de5\u5177',
  'tool':       '\u5b9e\u7528\u5de5\u5177',
  'popular':    '\u793e\u533a\u70ed\u95e8\u9879\u76ee',
  'content':    '\u5185\u5bb9\u521b\u4f5c',
  'video':      '\u89c6\u9891\u751f\u6210',
  'multi-agent':'\u591a Agent \u534f\u4f5c',
  'mood-tracker':'\u5fc3\u60c5\u8ffd\u8e2a\u5668',
  'snake':      '\u7ecf\u5178\u8d2a\u5403\u86c7',
  'distributed':'\u5206\u5e03\u5f0f\u67b6\u6784',
  'node':       'Node.js \u5f00\u53d1',
  'fullstack':  '\u5168\u6808\u5f00\u53d1',
  'platform':   '\u5e73\u53f0\u578b\u9879\u76ee',
  'mobile':     '\u79fb\u52a8\u5e94\u7528',
  'self-hosted':'\u81ea\u6258\u7ba1\u90e8\u7f72',
  'monitoring': '\u76d1\u63a7\u4e0e\u544a\u8b66',
  'security':   '\u5b89\u5168\u5408\u89c4',
  'devops':     'DevOps \u5168\u6d41\u7a0b',
  'helm':       'Helm \u5305\u7ba1\u7406',
  'cncf':       'CNCF \u751f\u6001\u9879\u76ee',
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
  var tplSourceRepo = t.source_repo || '';
  var tplUrl = t.html_url || '';
  if (!tplUrl && tplSourceRepo) {
    tplUrl = tplPlatform === 'github'
      ? 'https://github.com/' + tplSourceRepo
      : 'https://gitcode.com/' + tplSourceRepo;
  } else if (!tplUrl && tplSource === 'local') {
    tplUrl = 'https://gitcode.com/grape-dev/grape-template/tree/main/' + (t.id || '');
  }

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

  var highlights = [];
  tplTags.forEach(function(tg) {
    var desc = _TAG_HIGHLIGHTS[tg.toLowerCase()];
    if (desc && highlights.indexOf(desc) < 0) highlights.push(desc);
  });
  if (tplStats.language && !highlights.some(function(h) { return h.indexOf(tplStats.language) >= 0; })) {
    var langDesc = _TAG_HIGHLIGHTS[(tplStats.language || '').toLowerCase()];
    if (langDesc) highlights.push(langDesc);
  }
  var highlightsHtml = highlights.length
    ? highlights.map(function(h) {
        return '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">' +
          '<span style="color:var(--a);font-size:10px;flex-shrink:0">\u25c6</span>' +
          '<span style="font-size:11px;color:var(--t2);line-height:1.5">' + h + '</span>' +
        '</div>';
      }).join('')
    : '';

  var stages = _PIPELINE_STAGES[tplScene] || _PIPELINE_STAGES['default'];
  var stagesHtml = stages.map(function(s, i) {
    var num = i + 1;
    var isLast = i === stages.length - 1;
    var arrow = isLast ? '' : '<span style="color:var(--t3);font-size:10px;margin:0 3px">\u2192</span>';
    return '<span style="display:inline-flex;align-items:center"><span style="width:16px;height:16px;border-radius:50%;background:var(--a);color:#fff;font-size:9px;display:flex;align-items:center;justify-content:center;flex-shrink:0">' + num + '</span><span style="font-size:11px;color:var(--t2);margin-left:3px">' + s + '</span></span>' + arrow;
  }).join('');

  var externalLink = tplUrl
    ? '<a href="' + tplUrl + '" target="_blank" style="font-size:10px;color:var(--a);text-decoration:none;margin-left:8px">\u67e5\u770b\u539f\u9879\u76ee \u2197</a>'
    : '';

  var previewHtml = '<div id="ptPreview" style="background:var(--bg);border-radius:10px;padding:40px 20px;text-align:center;height:220px;display:flex;align-items:center;justify-content:center"><span style="font-size:48px;opacity:.3">' + tplIcon + '</span></div>';

  var modal = document.createElement('div');
  modal.id = 'ptCreateModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:stretch;justify-content:center;padding:0;z-index:999';
  modal.innerHTML =
    '<div class="cd" style="width:100%;max-width:900px;margin:0 auto;padding:0;border-radius:12px;overflow:hidden;display:flex;flex-direction:column;height:100vh">' +

      '<div style="padding:20px 32px 14px;flex-shrink:0;border-bottom:1px solid var(--b)">' +
        '<div style="display:flex;align-items:center;justify-content:space-between">' +
          '<div style="display:flex;align-items:center;gap:14px;flex:1;min-width:0">' +
            '<span style="font-size:36px;flex-shrink:0">' + tplIcon + '</span>' +
            '<div style="flex:1;min-width:0">' +
              '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px">' +
                '<span style="font-size:20px;font-weight:700;color:var(--t)">' + tplName + '</span>' +
                platformTag +
              '</div>' +
              '<div style="display:flex;align-items:center;gap:12px;margin-top:5px;flex-wrap:wrap">' +
                sourceBadge + starsHtml + langHtml + externalLink +
              '</div>' +
            '</div>' +
          '</div>' +
          '<span style="font-size:22px;color:var(--t3);cursor:pointer;flex-shrink:0;padding:4px 8px" onclick="document.getElementById(\'ptCreateModal\').remove()">\u00d7</span>' +
        '</div>' +
      '</div>' +

      '<div style="flex:1;min-height:0;overflow-y:auto;padding:20px 32px;display:flex;flex-direction:column;gap:18px">' +

        (previewHtml
          ? '<div style="flex-shrink:0">' + previewHtml + '</div>'
          : '') +

        '<div style="flex-shrink:0">' +
          '<div style="font-size:13px;font-weight:600;color:var(--t);margin-bottom:8px">\ud83d\udcdd \u6a21\u677f\u63cf\u8ff0</div>' +
          '<p style="font-size:13px;color:var(--t2);line-height:1.7">' + tplDesc + '</p>' +
        '</div>' +

        (highlightsHtml
          ? '<div style="flex-shrink:0">' +
              '<div style="font-size:13px;font-weight:600;color:var(--t);margin-bottom:10px">\u2728 \u6a21\u677f\u4eae\u70b9</div>' +
              highlightsHtml +
            '</div>'
          : '') +

        '<div style="flex:1;min-height:120px;display:flex;flex-direction:column">' +
          '<div style="font-size:13px;font-weight:600;color:var(--t);margin-bottom:10px;flex-shrink:0">\ud83d\udcd6 \u9879\u76ee\u6587\u6863</div>' +
          '<div id="ptReadme" style="background:var(--bg);border-radius:8px;padding:14px 16px;flex:1;overflow-y:auto;font-size:12px;line-height:1.7;color:var(--t2);white-space:pre-wrap">' +
            '<span style="color:var(--t3)">\u52a0\u8f7d\u4e2d...</span>' +
          '</div>' +
        '</div>' +

        '<div style="flex-shrink:0">' +
          '<div style="font-size:13px;font-weight:600;color:var(--t);margin-bottom:10px">\ud83d\udca1 \u6d41\u6c34\u7ebf\u9884\u89c8\uff08' + tplScene + '\uff09</div>' +
          '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:0;background:var(--bg);padding:12px 14px;border-radius:8px">' +
            stagesHtml +
          '</div>' +
        '</div>' +

        (tplTags.length
          ? '<div style="flex-shrink:0;display:flex;gap:5px;flex-wrap:wrap">' + tplTags.map(function(tg) {
              return '<span class="tg tg-d" style="font-size:11px">#' + tg + '</span>';
            }).join('') + '</div>'
          : '') +
        (reasonHtml ? '<div style="flex-shrink:0">' + reasonHtml + '</div>' : '') +
      '</div>' +

      '<div style="padding:14px 32px;flex-shrink:0;border-top:1px solid var(--b);display:flex;align-items:center;justify-content:space-between;gap:16px;background:var(--c)">' +
        '<div style="flex:1;min-width:0;display:flex;align-items:center;gap:12px">' +
          '<label class="ts t2" style="font-size:13px;white-space:nowrap;flex-shrink:0">' + I18N.t('templates.repo_name') + '</label>' +
          '<input id="ptRepoName" value="' + defaultName + '" style="flex:1;padding:10px 14px;border:1px solid var(--b);border-radius:var(--rd);font-size:14px;outline:none;background:var(--bg);box-sizing:border-box">' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:8px;flex-shrink:0">' +
          (tplUrl ? '<button class="bt bt-o bt-s" onclick="window.open(\'' + tplUrl + '\',\'_blank\')">\u67e5\u770b\u6e90\u4ee3\u7801 \u2197</button>' : '') +
          '<button class="bt bt-o bt-s" onclick="document.getElementById(\'ptCreateModal\').remove()">' + I18N.t('templates.cancel') + '</button>' +
          '<button class="bt bt-p bt-s" id="ptCreateBtn" onclick="doCreateFromTemplate(\'' + templateId + '\')" style="padding:8px 24px">' + I18N.t('templates.create_from_template_arrow') + '</button>' +
        '</div>' +
      '</div>' +

    '</div>';
  document.body.appendChild(modal);
  document.getElementById('ptRepoName').focus();
  document.getElementById('ptRepoName').select();

  API.get('/project-templates/' + encodeURIComponent(templateId) + '/readme').then(function(data) {
    var el = document.getElementById('ptReadme');
    if (!el) return;
    var content = (data.content || '').trim();
    if (!content) {
      el.innerHTML = '<span style="color:var(--t3)">\u8be5\u6a21\u677f\u6682\u65e0\u6587\u6863</span>';
      return;
    }
    el.textContent = content;
  }).catch(function() {
    var el = document.getElementById('ptReadme');
    if (el) el.innerHTML = '<span style="color:var(--t3)">\u6587\u6863\u52a0\u8f7d\u5931\u8d25</span>';
  });

  API.get('/project-templates/' + encodeURIComponent(templateId) + '/preview').then(function(data) {
    var el = document.getElementById('ptPreview');
    if (!el) return;
    var content = (data.content || '').trim();
    if (!content) return;
    el.outerHTML = content;
  }).catch(function() {});
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
        var _rk = r.repo_key || (r.provider === 'github' ? 'github:' : 'gitcode:') + r.full_name;
        setTimeout(function() { selectRepo(r.full_name, parts[0], parts[1], _rk); }, 900);
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
