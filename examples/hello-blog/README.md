# Hello Mini Blog

极简 Flask 博客骨架，包含首页与文章详情页。

## 运行

```bash
pip install -r requirements.txt
python app.py
```

访问 http://localhost:5000

## 学习要点

- Flask 动态路由 `/post/<int:pid>`
- `render_template_string` 模板渲染
- 404 处理与简单列表展示
