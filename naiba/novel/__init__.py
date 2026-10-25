# -*- coding: utf-8 -*-
"""小说域：作品与章节的持久化 + 工作台 API 门面（阶段一，零 AI）。

边界（《小说改编工作台总计划》§5.2、§6.1）：
- 作品数据统一在 ``data_dir/studio/novel/<book-id>/``，不另建 ``data_dir/novel/``。
- 本模块只管文件与数据，不 import 会话 / Run / 模型模块；阶段一是文件处理工具，
  **不创建 Studio 任务**（导入、拆章、勾选、保存、编辑都在一次请求或页面交互内完成）。
- 落盘沿用 ``naiba/studio/custom.py`` 的范式：路径白名单正则 + ``is_relative_to`` 越界
  校验 + 同目录临时文件 ``os.replace`` 原子替换。studio 下的写路径不在
  ``tests/test_sqlite_write_retry.py`` 的 AST 守门范围内，所以这里自觉照做。

两段式是硬契约（§5.1）：导入与作品详情只回**索引**，正文只有
``GET /api/novel/books/<id>/chapters/<i>`` 给。整本回传会撑爆上下文。

数据形状::

    data_dir/studio/novel/
    ├── _imports/<import-id>/          # 导入暂存（未入库，过期可清）
    │   ├── source.txt                 # 源文件副本（保证脚本/工作台结果一致）
    │   └── meta.json
    └── <book-id>/
        ├── book.json                  # 清单（权威来源）
        └── chapters/0001.json         # 单章一文件：改一章不重写整本
"""
