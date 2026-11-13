// D108-b（登录按钮零延迟·共享构建器）：OAuth 登录按钮的图标表与 DOM 模板。
// 本文件【同步加载】（无 defer）——index.html 在 #oauthBtns 容器后内联同步调用
// window.__renderOAuthButtonList，按钮随 HTML 解析进首帧；app.js 的对账路径
// （_applyProvidersToDOM → needsRender 重排）复用同一函数，单一来源不复制。
// 独立性约束：不得依赖 app.js 的符号（_esc/_provBrandColor/I18N 均做守卫降级）。
(function (global) {
  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  var _OAUTH_ICONS = {
    gitcode: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="m15.585 4.586.486-.274q.032.17.06.303c.032.158.06.289.072.418.103 1.118.665 1.941 1.462 2.127 1.165.27 2.264-.177 2.856-1.164.711-1.184.403-2.634-.808-3.507C16.346.061 12.647-.609 8.663.56.072 3.095-2.867 13.65 3.23 20.122c2.608 2.769 5.92 3.964 9.68 3.873 4.817-.113 8.285-2.513 10.5-6.674 1.57-2.952-.137-6.178-3.405-6.849a21 21 0 0 0-5.675-.362 4.8 4.8 0 0 0-1.805.548c-.625.325-.805.998-.735 1.666.065.608.531.972 1.086 1.064 1.118.175 2.25.277 3.378.37.327.027.657.03.986.033.473.005.944.01 1.405.086 1.314.217 1.766 1.284 1.09 2.425a4.7 4.7 0 0 1-.577.766 6.55 6.55 0 0 1-3.318 1.964c-2.333.57-4.669.603-6.99-.13-2.645-.835-4.221-2.777-4.277-5.392A9.1 9.1 0 0 1 5.76 8.907c.36-.654.558-1.327.503-2.067a26 26 0 0 1-.05-.972l-.025-.565q.401.084.792.212c1.011.406 2.007.592 3.102.294a5.6 5.6 0 0 1 1.902-.122 4.76 4.76 0 0 0 2.921-.714c.218-.128.439-.251.681-.387"/></svg>',
    github: '<svg width="16" height="16" viewBox="0 0 1024 1024" fill="currentColor"><path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667"/></svg>',
    gitee: '<svg width="16" height="16" viewBox="0 0 24 24"><circle cx="12" cy="12" r="12" fill="#C71D23"/><path fill="#FFFFFF" d="M18.074 5.333c.328 0 .593.266.592.593v1.482a.594.594 0 0 1-.593.592H9.777c-.982 0-1.778.796-1.778 1.778v5.63c0 .327.266.592.593.592h5.63c.982 0 1.778-.796 1.778-1.778v-.296a.593.593 0 0 0-.592-.593h-4.15a.592.592 0 0 1-.592-.592v-1.482a.593.593 0 0 1 .593-.592h6.815c.327 0 .593.265.593.592v3.408a4 4 0 0 1-4 4H5.926a.593.593 0 0 1-.593-.593V9.778a4.444 4.444 0 0 1 4.445-4.444h8.296Z"/></svg>',
    hwcloud: '<svg data-brand="huawei" width="16" height="16" viewBox="0 0 1024 1024" fill="currentColor"><g transform="matrix(1 0 0 -1 0 897)"><path fill="#C7000B" d="M512 896Q616 896 711 856Q803 817 874.0 746.0Q945 675 984 583Q1024 488 1024.0 384.0Q1024 280 984 185Q945 93 874.0 22.0Q803 -49 711 -88Q616 -128 512.0 -128.0Q408 -128 313 -88Q221 -49 150.0 22.0Q79 93 40 185Q0 280 0.0 384.0Q0 488 40 583Q79 675 150.0 746.0Q221 817 313 856Q408 896 512 896Z"/><path fill="#FFFFFF" d="M352 190H331V134Q331 115 337 109Q348 96 374.0 96.0Q400 96 412 109Q418 115 418 134V190H397V134Q397 122 392.0 116.5Q387 111 374.5 111.0Q362 111 357.0 116.5Q352 122 352 134ZM800 190H778V97H800ZM759 190H724Q700 190 688.5 178.0Q677 166 677.0 141.5Q677 117 693 105Q699 100 707 99Q713 97 724 97H759V114H725Q712 114 706.5 119.0Q701 124 701 136H759V152H701Q702 174 725 174H759ZM539 190H516L550 97H575L594 166L615 97H641L672 190H651L629 119L607 190H582L562 119ZM487 190H464L422 97H444L453 119H497L506 97H529ZM245 190H224V97H245V135H292V97H313V190H292V152H245ZM475 171 490 135H460ZM570 299Q570 299 569.5 299.0Q569 299 568 298L569 295Q588 281 613 266Q646 245 662 240L670 239Q680 238 690 241Q704 245 718 256Q734 269 748 293ZM453 299 276 293Q292 266 313 251Q337 235 361.5 240.5Q386 246 455 295Q456 297 456 298Q455 299 453 299ZM229 449Q220 422 226 395Q229 381 234 372Q246 347 269 331Q281 323 290 320Q307 313 323 312Q325 311 419 312H456L458 313Q459 314 458 315L457 316Q409 349 314 402ZM794 449 784 443Q768 435 761 431L720 408Q688 390 651 368Q646 366 635 359L626 353Q606 341 602.5 339.0Q599 337 593.0 333.5Q587 330 583.5 328.0Q580 326 574.5 322.0Q569 318 567.0 316.0Q565 314 566.5 313.0Q568 312 571 313H708Q746 319 770 343Q779 353 787 366Q793 377 797 392Q798 397 799 407L800 413Q800 420 799 424Q800 430 798 436L797 438Q796 446 794 449ZM312 596Q306 589 299 582Q285 564 276 546Q264 521 265 500Q266 484 274 469Q279 458 287 449L295 442Q323 415 389 377Q430 353 470 333L472 334Q473 335 473 336Q430 428 367 521Q335 568 312 596ZM712 596Q699 579 685 561Q653 518 623 470Q582 402 551 336H552Q551 335 552 334H555Q593 353 635 377Q701 415 729 442L730 443Q739 451 746 462Q757 480 758 501Q760 531 736 567Q724 584 712 596ZM554 672Q551 667 549 663Q542 650 537 632Q530 606 525 573Q520 532 520 481Q520 421 527 350Q527 348 529 348Q530 348 531 349Q562 394 586 436Q630 512 640 556L642 566Q644 578 644 588Q644 602 640 613L635 624Q629 638 618 648Q603 662 582 667ZM470 672Q455 670 442 667Q421 662 406 648Q395 638 388 624L384 613Q378 598 381 576Q382 565 384 556Q394 512 438 436Q462 394 492 349H493Q494 348 495 348H496Q496 349 496 350H497Q508 463 501 547Q496 606 482 645Q475 665 470 672Z"/></g></svg>',

  };

  function _oauthIcon(provider, name) {
    // 已知平台用内置 SVG，未知平台用首字母降级
    if (provider === 'huawei') provider = 'hwcloud'; // 键归一：绑定体系用 huawei（2026-11-08）
    if (_OAUTH_ICONS[provider]) return _OAUTH_ICONS[provider];
    var letter = (name || provider || '?')[0].toUpperCase();
    return '<span style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;font-size:11px;font-weight:700;line-height:1">' + _esc(letter) + '</span>';
  }


  function _renderOAuthButtonList(container, providers) {
        // D102-B: 登录按钮溢出折叠（参考 gitee.com/login）——固定序前 4 个
        // 直接展示，第 5 个起收进"..."展开按钮（点击展开剩余全部）
        var MAX_VISIBLE = 4;
        var _mkBtn = function(p) {
          var icon = _oauthIcon(p.provider, p.name);
var i18nKey = 'app.login.' + p.provider + '_login';
          // 台阶2：品牌色收口平台卡目录（_provBrandColor，同源）——
          // 前端 _brandOverride 硬编码表删除（与目录同源后删重）；渠道卡
          // brand_color（auth/providers.py）为 VCS 卡未覆盖时（hwcloud 等纯登录渠道）兜底。
          var _brandColor = (window._provBrandColor ? window._provBrandColor(p.provider) : '') || p.brand_color || '';
          var brandVar = _brandColor ? ' style="--brand:' + _brandColor + '"' : '';
          var ariaVar = p.configured ? '' : ' aria-disabled="true"';
          // D102-K: gitee 风格 icon-only（纯 logo 圆形，title/aria 提示平台名）
          // D102-N: 渠道卡 name 为英文代号时前端显示名映射（华为云/极狐等）
          var _NAME_MAP = {hwcloud: '华为云', huawei: '华为云', gitcode: 'GitCode',
                           gitee: 'Gitee', github: 'GitHub', gitlab: 'GitLab'};
          var _displayName = _NAME_MAP[p.provider] || p.name;
          // i18n 双通道（D107-1 收口）：渲染时点用 I18N.t 即时组装（语言包已就绪的
          // 常规场景）；同时挂 data-i18n-title/data-i18n-vars——语言包晚到（首帧
          // providers 先于 pack 返回）或后续切换语言时，applyI18nToDOM 统一重刷，
          // 不依赖本函数再次执行。huawei 有专名翻译不套 {name}。
          var _isHw = (p.provider === 'huawei' || p.provider === 'hwcloud');
          var _titleKey = _isHw ? 'app.login.oauth_title_huawei' : 'app.login.oauth_title';
          // 非 huawei：data-i18n-title='KEY' data-i18n-vars='{"name":"X"}'（双引号包裹 title，
          // 单引号包裹 vars JSON——与静态预置按钮同款转义结构）
          var _i18nAttrs = _isHw
            ? 'data-i18n-title="' + _titleKey + '"'
            : 'data-i18n-title="' + _titleKey + '" data-i18n-vars=\'{"name":"' + _displayName + '"}\'';
          var _loginTitle;
          if (_isHw) {
            _loginTitle = (typeof I18N !== 'undefined' && I18N.t) ? I18N.t('app.login.oauth_title_huawei') : '';
          } else {
            _loginTitle = ((typeof I18N !== 'undefined' && I18N.t && I18N.t('app.login.oauth_title')) || '使用 {name} 账号登录').replace('{name}', _displayName);
          }
          // 口径 v7 通用 DOM（用户裁定，gitee.com/login 形态）：纯品牌圆标，
          // 平台名走 title/aria 提示（hover 显示「使用XX账号登录」），无常显文字
          return '<button class="oauth-btn icon-only" ' + _i18nAttrs
            + ' title="' + _esc(_loginTitle) + '"'
            + ' aria-label="' + _esc(_loginTitle) + '"'
            + ' onclick="app.oauthLogin(\'' + p.provider + '\')"'
            + ariaVar + brandVar + '>' + icon
            + '<span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">' + _esc(_loginTitle) + '</span></button>';
        };
        if (providers.length > MAX_VISIBLE) {
          var visible = providers.slice(0, MAX_VISIBLE);
          var overflow = providers.slice(MAX_VISIBLE);
          container.innerHTML = visible.map(_mkBtn).join('')
            + '<button class="oauth-btn icon-only oauth-more-btn" id="oauthMoreBtn" title="more" '
            + 'onclick="var m=document.getElementById(\'oauthOverflowWrap\');'
            + 'var open=(m.style.display!==\'none\');m.style.display=open?\'none\':\'flex\';'
            + 'this.textContent=open?\'...\':\'‹\';" title="more">...</button>'
            + '<span id="oauthOverflowWrap" style="display:none;flex-wrap:wrap;gap:inherit">'
            + overflow.map(_mkBtn).join('') + '</span>';
        } else {
          container.innerHTML = providers.map(_mkBtn).join('');
        }
    if (typeof I18N !== 'undefined' && I18N.applyI18nToDOM) I18N.applyI18nToDOM();
  }

  global.__renderOAuthButtonList = _renderOAuthButtonList;
  global.__oauthIcon = _oauthIcon;
})(window);
