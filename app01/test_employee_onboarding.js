/* 本地控件状态回归，不需要打开浏览器或连接数据库。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'static/js/employee-onboarding.js'), 'utf8');
function setup(checked, readyState = 'complete', missing = '') {
    const handlers = {};
    const checkbox = {checked, addEventListener: (event, handler) => {handlers[event] = handler;}};
    const fields = {hidden: null, disabled: null};
    const document = {
        readyState,
        addEventListener: (event, handler) => {handlers[event] = handler;},
        getElementById: (id) => {
            if (id === missing) return null;
            return id === 'id_create_login' ? checkbox : fields;
        },
    };
    vm.runInNewContext(source, {document});
    return {checkbox, fields, handlers};
}
let state = setup(true);
assert.equal(state.fields.hidden, false);
assert.equal(state.fields.disabled, false);
state = setup(false);
assert.equal(state.fields.hidden, true);
assert.equal(state.fields.disabled, true);
state.checkbox.checked = true;
state.handlers.change();
assert.equal(state.fields.hidden, false);
assert.equal(state.fields.disabled, false);
state.checkbox.checked = false;
state.handlers.change();
assert.equal(state.fields.hidden, true);
setup(true, 'complete', 'employee-login-fields');
setup(true, 'complete', 'id_create_login');
state = setup(true, 'loading');
assert.equal(state.fields.hidden, null);
state.handlers.DOMContentLoaded();
assert.equal(state.fields.hidden, false);
console.log('Employee onboarding JavaScript: 7 scenarios passed.');
