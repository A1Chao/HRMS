/* Run with: node app01/test_list_filters.js */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'static/js/list-filters.js'), 'utf8');
let assertions = 0;
function control(form) {
    return {form, events: {}, addEventListener(event, handler) { this.events[event] = handler; }};
}
function install(controls) {
    vm.runInNewContext(source, {document: {querySelectorAll(selector) {
        assert.equal(selector, '[data-auto-submit]');
        return controls;
    }}});
}
for (const name of ['date', 'month', 'department_id', 'position']) {
    let submitted = 0;
    const filters = {q: '王', department_id: '1', position: '技术部经理', date: '', month: ''};
    const form = {checkValidity: () => true, requestSubmit() { submitted++; }};
    const input = control(form);
    install([input]);
    filters[name] = name === 'date' ? '2026-11-06' : 'selected-value';
    input.events.change();
    assert.equal(submitted, 1);
    assert.equal(filters.q, '王');
    filters[name] = '';
    input.events.change();
    assert.equal(submitted, 2, 'clearing a filter submits too');
    assertions++;
}
let submitted = 0;
const invalid = control({checkValidity: () => false, requestSubmit() { submitted++; }});
const detached = control(null);
install([invalid, detached]);
invalid.events.change();
detached.events.change();
assert.equal(submitted, 0);
assertions++;
const legacy = control({checkValidity: () => true, submit() { submitted++; }});
install([legacy]);
legacy.events.change();
assert.equal(submitted, 1);
assertions++;
install([]);
assertions++;
console.log('Automatic filter JavaScript: ' + assertions + ' scenarios passed.');
