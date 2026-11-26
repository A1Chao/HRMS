/* 只控制显示；是否允许创建账号、密码与角色校验全部由后端负责。 */
(function () {
    'use strict';
    function bindEmployeeLogin() {
        var checkbox = document.getElementById('id_create_login');
        var fields = document.getElementById('employee-login-fields');
        if (!checkbox || !fields) return;
        function sync() {
            fields.hidden = !checkbox.checked;
            fields.disabled = !checkbox.checked;
        }
        checkbox.addEventListener('change', sync);
        sync();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindEmployeeLogin);
    } else {
        bindEmployeeLogin();
    }
}());
