"""
分页工具：先解析 page，再统计总数，最后对 QuerySet 做当前页切片。
page_queryset 是要显示的记录，page_html() 是分页链接；链接保留搜索和筛选参数。
分页必须在数据库稳定排序之后执行，不能先取一页再把领导排到前面。
"""
import copy

from django.utils.html import escape


class Pagination(object):
    """ 分页公共组件（对应教程 utils/pagination.py）。

    用法：
        page_object = Pagination(request, queryset)                 # 每页条数用默认值
        page_object = Pagination(request, queryset, page_size=20)   # 也可以单独指定
        page_object.page_queryset   # 当前页的数据
        page_object.page_html()     # 生成好的 Bootstrap 分页 HTML
    """

    def __init__(self, request, queryset, page_size=5, page_param="page", plus=5):
        """
        :param request: 请求对象（为了拿到 ?page= 和搜索条件）
        :param queryset: 数据库查询结果
        :param page_size: 每页显示多少条（改默认值就能全局调整，views 里不要重复传）
        :param page_param: URL 中分页参数的名字，例如 ?page=1
        :param plus: 当前页前后各显示多少个页码
        """
        # 保留原来的搜索条件：拷贝一份 request.GET，后面改页码时不会影响原对象
        query_dict = copy.deepcopy(request.GET)
        query_dict._mutable = True
        self.query_dict = query_dict

        self.page_param = page_param
        self.page_size = page_size
        self.plus = plus

        # 1. 读取 page 参数并做容错（?page=abc / 0 / 负数 / 超出总页数）
        page = request.GET.get(page_param, "1")
        if len(str(page)) <= 19 and str(page).isascii() and str(page).isdecimal():
            page = int(page)
        else:
            page = 1

        self.total_count = queryset.count()
        total_page_count, div = divmod(self.total_count, page_size)
        if div:
            total_page_count += 1
        self.total_page_count = total_page_count

        if page < 1 or total_page_count == 0:
            page = 1
        if total_page_count and page > total_page_count:
            page = total_page_count

        self.page = page
        self.start = (page - 1) * page_size
        self.end = page * page_size
        # QuerySet 切片生成 SQL LIMIT/OFFSET；这里不把全库记录一次装进内存。
        self.page_queryset = queryset[self.start:self.end]

    def _page_url(self, page):
        """ 生成“跳到第 page 页”的查询串，同时带上原来的搜索条件（例如 q=xxx）。 """
        self.query_dict.setlist(self.page_param, [page])
        return self.query_dict.urlencode()

    def page_html(self):
        """ 生成分页的 <li> 列表（模板里放在 <ul class="pagination"> 中）。

        顺序和教程一致：首页 / 上一页 / 页码 / 下一页 / 尾页 / 页码输入框 + 跳转按钮。
        只有一条数据（只有 1 页）时也照样显示，只是前后按钮是灰色不可点的状态。
        """
        if self.total_page_count == 0:
            return ""

        html_list = []

        # 首页
        if self.page > 1:
            html_list.append('<li><a href="?{}">首页</a></li>'.format(self._page_url(1)))
        else:
            html_list.append('<li class="disabled"><a href="#">首页</a></li>')

        # 上一页
        if self.page > 1:
            html_list.append('<li><a href="?{}">上一页</a></li>'.format(self._page_url(self.page - 1)))
        else:
            html_list.append('<li class="disabled"><a href="#">上一页</a></li>')

        # 页码：当前页前后各 plus 个
        page_start = max(1, self.page - self.plus)
        page_end = min(self.total_page_count, self.page + self.plus)
        for i in range(page_start, page_end + 1):
            if i == self.page:
                html_list.append('<li class="active"><a href="#">{}</a></li>'.format(i))
            else:
                html_list.append('<li><a href="?{}">{}</a></li>'.format(self._page_url(i), i))

        # 下一页
        if self.page < self.total_page_count:
            html_list.append('<li><a href="?{}">下一页</a></li>'.format(self._page_url(self.page + 1)))
        else:
            html_list.append('<li class="disabled"><a href="#">下一页</a></li>')

        # 尾页
        if self.page < self.total_page_count:
            html_list.append('<li><a href="?{}">尾页</a></li>'.format(self._page_url(self.total_page_count)))
        else:
            html_list.append('<li class="disabled"><a href="#">尾页</a></li>')

        # 页码输入框 + 跳转按钮：
        # 用一个 GET 表单，把“搜索条件”等其它参数用 hidden 一起提交，
        # 这样跳页后 q 不会丢，而且不需要额外写 JS。
        hidden_inputs = "".join(
            '<input type="hidden" name="{}" value="{}">'.format(escape(name), escape(value))
            for name, value in self.query_dict.items()
            if name != self.page_param
        )
        html_list.append(
            '<li style="display: inline-block; vertical-align: middle;">'
            '<form method="get" style="margin: 0">'
            '{}'
            '<div class="input-group" style="width: 180px;">'
            '<input type="text" name="{}" class="form-control" placeholder="页码">'
            '<span class="input-group-btn">'
            '<button class="btn btn-default" type="submit">跳转</button>'
            '</span>'
            '</div>'
            '</form>'
            '</li>'.format(hidden_inputs, self.page_param)
        )

        return "".join(html_list)