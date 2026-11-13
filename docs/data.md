# 来源资料约定

pages.json 中 now、last_visit、interval 与 max_staleness 的单位均为虚拟天数，daily_budget 表示当日允许发出的访问数。last_visit 为 null 的地址没有历史。来源的 activity 有两版正文，manual 内容稳定，broken 返回 503。http_source 可复现条件请求和手动推进来源版本。
