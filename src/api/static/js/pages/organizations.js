// 组织管理页面（i18n）
function renderOrganizations() {
  var pc = document.getElementById('pageContent');
  if (!pc) return;
  pc.innerHTML = '<div style="padding:20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">' +
    '<h2 style="margin:0;font-size:18px">' + I18N.t("organizations.title") + '</h2>' +
    '<button onclick="_showCreateOrgDialog()" style="padding:8px 16px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:13px">' + I18N.t("organizations.create_org") + '</button>' +
    '</div><div id="orgList" style="display:flex;flex-direction:column;gap:12px"></div></div>';
  _loadOrgs();
}

function _loadOrgs() {
  var container = document.getElementById('orgList');
  if (!container) return;
  container.innerHTML = '<div style="color:var(--t3);padding:20px">' + I18N.t("organizations.loading") + '</div>';
  API.get('/orgs').then(function(d) {
    if (!d || d.length === 0) {
      container.innerHTML = '<div style="color:var(--t3);padding:20px;text-align:center">' + I18N.t("organizations.no_orgs") + '</div>';
      return;
    }
    var html = '';
    d.forEach(function(org) {
      var badge = org.is_personal ? '<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:var(--c);color:var(--t2);margin-left:6px">' + I18N.t("organizations.badge_personal") + '</span>' : '';
      var roleColor = org.role === 'owner' ? 'var(--g)' : org.role === 'admin' ? 'var(--a)' : 'var(--t3)';
      html += '<div style="border:1px solid var(--b);border-radius:12px;padding:16px;display:flex;align-items:center;gap:12px;cursor:pointer" onclick="_showOrgDetail(' + org.id + ')">' +
        '<div style="width:40px;height:40px;border-radius:10px;background:var(--c);display:flex;align-items:center;justify-content:center;font-size:18px">🏢</div>' +
        '<div style="flex:1"><div style="font-weight:600;font-size:14px">' + (org.name || '') + badge + '</div>' +
        '<div style="font-size:12px;color:var(--t3)">' + (org.slug || '') + ' · ' + (org.member_count || 0) + ' ' + I18N.t("organizations.members") + '</div></div>' +
        '<div style="font-size:12px;color:' + roleColor + ';font-weight:500">' + (org.role || 'member') + '</div>' +
        '</div>';
    });
    container.innerHTML = html;
  }).catch(function(e) {
    container.innerHTML = '<div style="color:var(--r);padding:20px">' + I18N.t("organizations.load_failed", {msg: (e.message || e)}) + '</div>';
  });
}

function _showCreateOrgDialog() {
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.2);backdrop-filter:blur(3px);z-index:9999;opacity:0;transition:opacity .2s';
  var dlg = document.createElement('div');
  dlg.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) scale(1.05);background:var(--c,#fff);border-radius:12px;padding:20px;max-width:360px;width:90%;box-shadow:0 6px 30px rgba(0,0,0,.12);font-family:-apple-system,BlinkMacSystemFont,sans-serif;transition:transform .2s cubic-bezier(.4,0,.2,1)';
  dlg.innerHTML =
    '<div style="font-size:14px;font-weight:600;margin-bottom:12px">' + I18N.t("organizations.create.title") + '</div>' +
    '<div style="margin-bottom:10px"><label style="font-size:12px;color:var(--t2);display:block;margin-bottom:4px">' + I18N.t("organizations.create.name_label") + '</label>' +
    '<input id="orgCreateName" type="text" placeholder="' + I18N.t("organizations.create.name_placeholder") + '" style="width:100%;padding:8px;border:1px solid var(--b);border-radius:6px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box"></div>' +
    '<div style="margin-bottom:10px"><label style="font-size:12px;color:var(--t2);display:block;margin-bottom:4px">' + I18N.t("organizations.create.slug_label") + '</label>' +
    '<input id="orgCreateSlug" type="text" placeholder="' + I18N.t("organizations.create.slug_placeholder") + '" style="width:100%;padding:8px;border:1px solid var(--b);border-radius:6px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box"></div>' +
    '<div style="margin-bottom:16px"><label style="font-size:12px;color:var(--t2);display:block;margin-bottom:4px">' + I18N.t("organizations.create.desc_label") + '</label>' +
    '<input id="orgCreateDesc" type="text" placeholder="' + I18N.t("organizations.create.desc_placeholder") + '" style="width:100%;padding:8px;border:1px solid var(--b);border-radius:6px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box"></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
    '<button id="_orgCancel" style="padding:8px 16px;border:none;border-radius:8px;background:rgba(0,0,0,.06);font-size:12px;cursor:pointer;color:var(--t1)">' + I18N.t("common.cancel") + '</button>' +
    '<button id="_orgOK" style="padding:8px 16px;border:none;border-radius:8px;background:var(--a);color:#fff;font-size:12px;cursor:pointer;font-weight:600">' + I18N.t("organizations.create.confirm") + '</button>' +
    '</div>';
  overlay.appendChild(dlg);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { overlay.style.opacity = '1'; dlg.style.transform = 'translate(-50%,-50%) scale(1)'; });
  function _close() { overlay.style.opacity = '0'; dlg.style.transform = 'translate(-50%,-50%) scale(1.05)'; setTimeout(function() { overlay.remove(); }, 200); }
  document.getElementById('_orgCancel').onclick = _close;
  document.getElementById('_orgOK').onclick = function() {
    var name = document.getElementById('orgCreateName').value.trim();
    var slug = document.getElementById('orgCreateSlug').value.trim();
    var desc = document.getElementById('orgCreateDesc').value.trim();
    if (!name) { app.showToast(I18N.t("organizations.create.name_required")); return; }
    var okBtn = document.getElementById('_orgOK');
    okBtn.disabled = true;
    okBtn.textContent = I18N.t("organizations.create.creating");
    API.post('/orgs', { name: name, slug: slug, description: desc }).then(function() {
      _close();
      app.showToast(I18N.t("organizations.create.success"));
      _loadOrgs();
    }).catch(function(e) {
      okBtn.disabled = false;
      okBtn.textContent = I18N.t("organizations.create.confirm");
      app.showToast(I18N.t("organizations.create.failed", {msg: (e.message || e)}));
    });
  };
  overlay.onclick = function(e) { if (e.target === overlay) _close(); };
}

function _showOrgDetail(orgId) {
  var pc = document.getElementById('pageContent');
  if (!pc) return;
  pc.innerHTML = '<div style="padding:20px"><div style="display:flex;align-items:center;gap:8px;margin-bottom:16px">' +
    '<a onclick="app.nav(\'organizations\')" style="cursor:pointer;color:var(--a);font-size:13px">' + I18N.t("organizations.detail.back") + '</a></div>' +
    '<div id="orgDetail" style="display:flex;flex-direction:column;gap:16px"></div></div>';

  Promise.all([
    API.get('/orgs/' + orgId),
    API.get('/orgs/' + orgId + '/members')
  ]).then(function(results) {
    var org = results[0];
    var members = results[1] || [];
    var d = document.getElementById('orgDetail');
    if (!d) return;

    var badge = org.is_personal ? '<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:var(--c);color:var(--t2);margin-left:6px">' + I18N.t("organizations.badge_personal") + '</span>' : '';
    var html = '<div style="border:1px solid var(--b);border-radius:12px;padding:16px">' +
      '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">' +
      '<div style="width:48px;height:48px;border-radius:12px;background:var(--c);display:flex;align-items:center;justify-content:center;font-size:22px">🏢</div>' +
      '<div><div style="font-weight:600;font-size:16px">' + (org.name || '') + badge + '</div>' +
      '<div style="font-size:12px;color:var(--t3)">' + (org.slug || '') + '</div></div></div>' +
      '<div style="font-size:13px;color:var(--t2);margin-bottom:4px">' + (org.description || I18N.t("organizations.detail.no_description")) + '</div>' +
      '<div style="font-size:12px;color:var(--t3)">' + I18N.t("organizations.detail.role_label") + ': ' + (org.role || '') + ' · ' + I18N.t("organizations.detail.member_limit") + ': ' + (org.max_members || 50) + ' · ' + I18N.t("organizations.detail.worker_limit") + ': ' + (org.max_workers || 10) + '</div>' +
      '</div>';

    // 成员列表
    html += '<div style="border:1px solid var(--b);border-radius:12px;padding:16px">' +
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">' +
      '<h3 style="margin:0;font-size:14px">' + I18N.t("organizations.detail.members_title") + ' (' + members.length + ')</h3>';
    if (org.role === 'owner' || org.role === 'admin') {
      html += '<button onclick="_showAddMemberDialog(' + orgId + ')" style="padding:6px 12px;border:none;border-radius:6px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' + I18N.t("organizations.detail.add_member") + '</button>';
    }
    html += '</div>';
    members.forEach(function(m) {
      var roleColor = m.role === 'owner' ? 'var(--g)' : m.role === 'admin' ? 'var(--a)' : 'var(--t3)';
      html += '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--b)">' +
        '<div style="width:32px;height:32px;border-radius:8px;background:var(--c);display:flex;align-items:center;justify-content:center;font-size:14px">' + (m.username || '?').charAt(0).toUpperCase() + '</div>' +
        '<div style="flex:1"><div style="font-size:13px;font-weight:500">' + (m.nickname || m.username || '') + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + (m.username || '') + '</div></div>' +
        '<span style="font-size:12px;color:' + roleColor + '">' + (m.role || '') + '</span>';
      if ((org.role === 'owner') && m.role !== 'owner') {
        html += '<button onclick="_removeMember(' + orgId + ',' + m.user_id + ',\'' + (m.username || '').replace(/'/g, "\\'") + '\')" style="margin-left:8px;padding:4px 8px;border:1px solid var(--r);border-radius:4px;background:transparent;color:var(--r);cursor:pointer;font-size:11px">' + I18N.t("common.remove") + '</button>';
      }
      html += '</div>';
    });
    html += '</div>';

    // 切换到该组织 + 编辑 + 删除组织
    html += '<div style="border:1px solid var(--b);border-radius:12px;padding:16px;display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap">';
    if (org.role === 'owner' || org.role === 'admin') {
      html += '<button onclick="_showEditOrgDialog(' + orgId + ',\'' + (org.name || '').replace(/'/g, "\\'") + '\',\'' + (org.description || '').replace(/'/g, "\\'") + '\',' + (org.max_members || 50) + ',' + (org.max_workers || 10) + ')" style="padding:10px 20px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + (I18N.t("organizations.detail.edit_org") || '编辑组织') + '</button>';
    }
    if (!org.is_personal && (org.role === 'owner')) {
      html += '<button onclick="_deleteOrg(' + orgId + ',\'' + (org.name || '').replace(/'/g, "\\'") + '\')" style="padding:10px 20px;border:1px solid var(--r);border-radius:8px;background:transparent;color:var(--r);cursor:pointer;font-size:12px">' + I18N.t("organizations.detail.delete_org") + '</button>';
    }
    html += '<button onclick="_switchOrg(' + orgId + ')" style="padding:10px 24px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:13px">' + I18N.t("organizations.detail.switch_to") + '</button>' +
      '</div>';

    d.innerHTML = html;
  }).catch(function(e) {
    document.getElementById('orgDetail').innerHTML = '<div style="color:var(--r)">' + I18N.t("organizations.load_failed", {msg: (e.message || e)}) + '</div>';
  });
}

function _showAddMemberDialog(orgId) {
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.2);backdrop-filter:blur(3px);z-index:9999;opacity:0;transition:opacity .2s';
  var dlg = document.createElement('div');
  dlg.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) scale(1.05);background:var(--c,#fff);border-radius:12px;padding:20px;max-width:340px;width:90%;box-shadow:0 6px 30px rgba(0,0,0,.12);font-family:-apple-system,BlinkMacSystemFont,sans-serif;transition:transform .2s cubic-bezier(.4,0,.2,1)';
  dlg.innerHTML =
    '<div style="font-size:14px;font-weight:600;margin-bottom:12px">' + I18N.t("organizations.add_member.title") + '</div>' +
    '<div style="margin-bottom:10px"><label style="font-size:12px;color:var(--t2);display:block;margin-bottom:4px">' + I18N.t("organizations.add_member.username_label") + '</label>' +
    '<div style="position:relative">' +
    '<input id="addMemberUsername" type="text" placeholder="' + I18N.t("organizations.add_member.username_placeholder") + '" style="width:100%;padding:8px;border:1px solid var(--b);border-radius:6px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box" oninput="_searchOrgUsers()">' +
    '<div id="addMemberSuggest" style="position:absolute;top:100%;left:0;right:0;max-height:160px;overflow-y:auto;border:1px solid var(--b);border-radius:6px;background:var(--c);display:none;z-index:100"></div>' +
    '</div></div>' +
    '<div style="margin-bottom:16px"><label style="font-size:12px;color:var(--t2);display:block;margin-bottom:4px">' + I18N.t("organizations.add_member.role_label") + '</label>' +
    '<select id="addMemberRole" style="width:100%;padding:8px;border:1px solid var(--b);border-radius:6px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box">' +
    '<option value="member">' + I18N.t("organizations.add_member.role_member") + '</option>' +
    '<option value="admin">' + I18N.t("organizations.add_member.role_admin") + '</option>' +
    '</select></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
    '<button id="_addMemberCancel" style="padding:8px 16px;border:none;border-radius:8px;background:rgba(0,0,0,.06);font-size:12px;cursor:pointer;color:var(--t1)">' + I18N.t("common.cancel") + '</button>' +
    '<button id="_addMemberOK" style="padding:8px 16px;border:none;border-radius:8px;background:var(--a);color:#fff;font-size:12px;cursor:pointer;font-weight:600">' + I18N.t("organizations.add_member.confirm") + '</button>' +
    '</div>';
  overlay.appendChild(dlg);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { overlay.style.opacity = '1'; dlg.style.transform = 'translate(-50%,-50%) scale(1)'; });
  function _close() { overlay.style.opacity = '0'; dlg.style.transform = 'translate(-50%,-50%) scale(1.05)'; setTimeout(function() { overlay.remove(); }, 200); }
  document.getElementById('_addMemberCancel').onclick = _close;
  document.getElementById('_addMemberOK').onclick = function() {
    var username = document.getElementById('addMemberUsername').value.trim();
    var role = document.getElementById('addMemberRole').value;
    if (!username) { app.showToast(I18N.t("organizations.add_member.username_required")); return; }
    var okBtn = document.getElementById('_addMemberOK');
    okBtn.disabled = true;
    okBtn.textContent = I18N.t("organizations.add_member.adding");
    API.post('/orgs/' + orgId + '/members', { username: username, role: role }).then(function() {
      _close();
      app.showToast(I18N.t("organizations.add_member.success"));
      _showOrgDetail(orgId);
    }).catch(function(e) {
      okBtn.disabled = false;
      okBtn.textContent = I18N.t("organizations.add_member.confirm");
      app.showToast(I18N.t("organizations.add_member.failed", {msg: (e.message || e)}));
    });
  };
  overlay.onclick = function(e) { if (e.target === overlay) _close(); };
}

var _orgUserSearchTimer = null;
function _searchOrgUsers() {
  clearTimeout(_orgUserSearchTimer);
  var input = document.getElementById('addMemberUsername');
  if (!input) return;
  var q = input.value.trim();
  var suggest = document.getElementById('addMemberSuggest');
  if (!suggest) return;
  if (q.length < 1) { suggest.style.display = 'none'; return; }
  _orgUserSearchTimer = setTimeout(function() {
    API.get('/orgs/search/users?q=' + encodeURIComponent(q)).then(function(d) {
      if (!d || d.length === 0) { suggest.style.display = 'none'; return; }
      var html = '';
      d.forEach(function(u) {
        html += '<div onclick="_selectOrgUser(' + u.id + ',\'' + (u.username || '').replace(/'/g, "\\'") + '\')" style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--b);font-size:12px">' +
          '<span style="font-weight:500">' + (u.username || '') + '</span>' +
          (u.nickname ? '<span style="color:var(--t3);margin-left:6px">' + u.nickname + '</span>' : '') +
          '</div>';
      });
      suggest.innerHTML = html;
      suggest.style.display = 'block';
    }).catch(function() { suggest.style.display = 'none'; });
  }, 200);
}

function _selectOrgUser(uid, username) {
  var input = document.getElementById('addMemberUsername');
  if (input) input.value = username;
  var suggest = document.getElementById('addMemberSuggest');
  if (suggest) suggest.style.display = 'none';
}

window._searchOrgUsers = _searchOrgUsers;
window._selectOrgUser = _selectOrgUser;

function _removeMember(orgId, userId, username) {
  if (window._grapeConfirm) {
    _grapeConfirm({
      title: I18N.t("organizations.remove_member.title"),
      message: I18N.t("organizations.remove_member.confirm_msg", {name: (username || userId)}),
      confirmText: I18N.t("common.remove"),
      danger: true,
      onConfirm: function() {
        API.del('/orgs/' + orgId + '/members/' + userId).then(function() {
          app.showToast(I18N.t("organizations.remove_member.success"));
          _showOrgDetail(orgId);
        }).catch(function(e) {
          app.showToast(I18N.t("organizations.remove_member.failed", {msg: (e.message || e)}));
        });
      }
    });
  } else {
    if (!confirm(I18N.t("organizations.remove_member.confirm_msg", {name: (username || userId)}))) return;
    API.del('/orgs/' + orgId + '/members/' + userId).then(function() {
      app.showToast(I18N.t("organizations.remove_member.success"));
      _showOrgDetail(orgId);
    }).catch(function(e) {
      app.showToast(I18N.t("organizations.remove_member.failed", {msg: (e.message || e)}));
    });
  }
}

function _switchOrg(orgId) {
  app.switchOrg(orgId);
}

function _deleteOrg(orgId, orgName) {
  if (window._grapeConfirm) {
    _grapeConfirm({
      title: I18N.t("organizations.delete_org.title"),
      message: I18N.t("organizations.delete_org.confirm_msg", {name: (orgName || orgId)}),
      confirmText: I18N.t("organizations.delete_org.confirm"),
      danger: true,
      onConfirm: function() {
        API.del('/orgs/' + orgId).then(function() {
          app.showToast(I18N.t("organizations.delete_org.success"));
          renderOrganizations();
        }).catch(function(e) {
          app.showToast(I18N.t("organizations.delete_org.failed", {msg: (e.message || e)}));
        });
      }
    });
  } else {
    if (!confirm(I18N.t("organizations.delete_org.confirm_msg", {name: (orgName || orgId)}))) return;
    API.del('/orgs/' + orgId).then(function() {
      app.showToast(I18N.t("organizations.delete_org.success"));
      renderOrganizations();
    }).catch(function(e) {
      app.showToast(I18N.t("organizations.delete_org.failed", {msg: (e.message || e)}));
    });
  }
}

window.renderOrganizations = renderOrganizations;
window._switchOrg = _switchOrg;
window._deleteOrg = _deleteOrg;
window._showEditOrgDialog = _showEditOrgDialog;

function _showEditOrgDialog(orgId, name, description, maxMembers, maxWorkers) {
  var existing = document.getElementById('_editOrgOverlay');
  if (existing) { existing.remove(); return; }
  var overlay = document.createElement('div');
  overlay.id = '_editOrgOverlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center';
  var dlg = document.createElement('div');
  dlg.style.cssText = 'position:relative;background:var(--c,#fff);border-radius:12px;padding:24px;max-width:400px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.15);font-family:-apple-system,BlinkMacSystemFont,sans-serif';
  dlg.innerHTML =
    '<div style="font-size:16px;font-weight:600;margin-bottom:16px;color:var(--t1)">' + (I18N.t("organizations.detail.edit_org") || '编辑组织') + '</div>' +
    '<div style="margin-bottom:12px"><label style="display:block;font-size:12px;color:var(--t3);margin-bottom:4px">' + (I18N.t("organizations.create.name_label") || '名称') + '</label>' +
    '<input id="_editOrgName" type="text" value="' + (name || '').replace(/"/g, '&quot;') + '" style="width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box;outline:none"></div>' +
    '<div style="margin-bottom:12px"><label style="display:block;font-size:12px;color:var(--t3);margin-bottom:4px">' + (I18N.t("organizations.create.desc_label") || '描述') + '</label>' +
    '<textarea id="_editOrgDesc" rows="2" style="width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box;outline:none;resize:none">' + (description || '') + '</textarea></div>' +
    '<div style="display:flex;gap:12px;margin-bottom:16px">' +
    '<div style="flex:1"><label style="display:block;font-size:12px;color:var(--t3);margin-bottom:4px">' + (I18N.t("organizations.detail.member_limit") || '成员上限') + '</label>' +
    '<input id="_editOrgMaxMembers" type="number" min="1" max="500" value="' + (maxMembers || 50) + '" style="width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box;outline:none"></div>' +
    '<div style="flex:1"><label style="display:block;font-size:12px;color:var(--t3);margin-bottom:4px">' + (I18N.t("organizations.detail.worker_limit") || 'Worker 上限') + '</label>' +
    '<input id="_editOrgMaxWorkers" type="number" min="0" max="100" value="' + (maxWorkers || 10) + '" style="width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box;outline:none"></div>' +
    '</div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end">' +
    '<button id="_editOrgCancel" style="padding:8px 16px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:13px">' + (I18N.t("common.cancel") || '取消') + '</button>' +
    '<button id="_editOrgSave" style="padding:8px 20px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:13px;font-weight:500">' + (I18N.t("common.save") || '保存') + '</button>' +
    '</div>';
  overlay.appendChild(dlg);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { overlay.style.opacity = '1'; });
  function _close() { overlay.remove(); }
  document.getElementById('_editOrgCancel').onclick = _close;
  overlay.onclick = function(e) { if (e.target === overlay) _close(); };
  document.getElementById('_editOrgSave').onclick = function() {
    var newName = document.getElementById('_editOrgName').value.trim();
    var newDesc = document.getElementById('_editOrgDesc').value.trim();
    var newMaxMembers = parseInt(document.getElementById('_editOrgMaxMembers').value) || 0;
    var newMaxWorkers = parseInt(document.getElementById('_editOrgMaxWorkers').value) || 0;
    if (!newName) { app.showToast(I18N.t("organizations.create.name_required") || '名称不能为空'); return; }
    var btn = this; btn.disabled = true; btn.textContent = '...';
    API.put('/orgs/' + orgId, { name: newName, description: newDesc, max_members: newMaxMembers, max_workers: newMaxWorkers }).then(function() {
      app.showToast(I18N.t("organizations.detail.edit_success") || '组织信息已更新');
      _close();
      _showOrgDetail(orgId);
    }).catch(function(e) {
      app.showToast((I18N.t("organizations.detail.edit_failed") || '更新失败') + ': ' + (e.message || e));
      btn.disabled = false; btn.textContent = (I18N.t("common.save") || '保存');
    });
  };
}
