"""Hello Flask —— 极简 Flask Web 应用"""
from flask import Flask

app = Flask(__name__)


@app.route("/")
def hello():
    return "<h1>Hello, World!</h1><p>这是一个极简 Flask 应用。</p>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
