/* 日期、月份、职位、部门共用的自动筛选；不是每输入一个姓名字符就请求服务器。 */
(function () {
    'use strict';
    // 模板加 data-auto-submit 才参与自动提交；不碰其他 POST 表单。
    document.querySelectorAll('[data-auto-submit]').forEach(function (control) {
        control.addEventListener('change', function () {
            var form = control.form;
            if (!form || !form.checkValidity()) {
                return;
            }
            // requestSubmit 保留浏览器校验和 submit 事件；GET 表单不含 page，因此变更后回第一页。
            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else {
                form.submit();
            }
        });
    });
}());
