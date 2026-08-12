"""Hello Mini Blog —— 极简 Flask 博客"""
from flask import Flask, render_template_string

app = Flask(__name__)

POSTS = [
    {"id": 1, "title": "我的第一篇博客", "content": "欢迎来到我的迷你博客！这是学习 Flask 的第一站。"},
    {"id": 2, "title": "Hello World", "content": "极简博客骨架，包含首页与文章页。"},
]

INDEX_TMPL = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Hello Mini Blog</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
.post{padding:14px 0;border-bottom:1px solid #eee}.post a{color:#2563eb;text-decoration:none}</style>
</head>
<body>
  <h1>📚 Hello Mini Blog</h1>
  {% for p in posts %}
    <div class="post"><a href="/post/{{ p.id }}">{{ p.title }}</a></div>
  {% endfor %}
</body>
</html>
"""

POST_TMPL = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{{ post.title }}</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px}
a{color:#2563eb}</style>
</head>
<body>
  <p><a href="/">← 返回首页</a></p>
  <h1>{{ post.title }}</h1>
  <p>{{ post.content }}</p>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_TMPL, posts=POSTS)


@app.route("/post/<int:pid>")
def post(pid):
    post = next((p for p in POSTS if p["id"] == pid), None)
    if post is None:
        return "<h1>404</h1><p>文章不存在</p>", 404
    return render_template_string(POST_TMPL, post=post)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
